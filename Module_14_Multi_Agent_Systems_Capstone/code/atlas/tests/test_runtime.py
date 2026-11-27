"""Unit tests for core/runtime — agent loop, approval gates, dispatcher.

Everything external is mocked: no Claude API calls, no Postgres, no
Telegram, no Ollama. The agent loop is exercised against a fake Anthropic
client whose messages.create yields a scripted sequence of responses.

We patch, per module:
  * core.runtime.agent.get_client     -> fake Anthropic client
  * core.runtime.agent.start_episode  -> returns a fixed uuid
  * core.runtime.agent.end_episode    -> no-op
  * core.runtime.agent.db.execute     -> no-op
and the telegram / db calls used by approval and dispatcher.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.runtime import approval as ap
from core.runtime import dispatcher as dp
from core.runtime.agent import AgentConfig, ToolSpec, run_agent

# ---------------------------------------------------------------------------
# Fake Anthropic response builders
# ---------------------------------------------------------------------------


def _usage(inp: int = 10, out: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use(tool_id: str, name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _response(content: list, stop_reason: str, usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, usage=usage or _usage()
    )


def _fake_client(responses) -> MagicMock:
    """Fake AsyncAnthropic whose messages.create yields `responses` in order."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    # The runtime goes through the beta namespace when a beta flag is set
    # (refusal fallbacks, MCP servers); both paths share one scripted mock.
    client.beta = MagicMock()
    client.beta.messages = MagicMock()
    client.beta.messages.create = client.messages.create
    return client


def _agent_env(client: MagicMock) -> ExitStack:
    """Patch agent.py's external dependencies. Use as a `with` context."""
    stack = ExitStack()
    stack.enter_context(
        patch("core.runtime.agent.get_client", return_value=client)
    )
    stack.enter_context(
        patch("core.runtime.agent.start_episode", new=AsyncMock(return_value=uuid4()))
    )
    stack.enter_context(
        patch("core.runtime.agent.end_episode", new=AsyncMock())
    )
    stack.enter_context(
        patch("core.runtime.agent.db.execute", new=AsyncMock())
    )
    stack.enter_context(
        patch("core.runtime.agent.projects_block", new=AsyncMock(return_value="- demo-dental: Demo Dental [p1 | client | active]"))
    )
    stack.enter_context(
        patch("core.runtime.agent.enforce_daily_cap", new=AsyncMock())
    )
    # The real config/context.yaml would add people and open loops to every
    # run; tests see an empty context pack.
    stack.enter_context(patch("core.context_pack.load", return_value={}))
    # run_agent imports these lazily at call time, so they are patched at
    # their source modules. Without this the tests silently depend on the
    # database being unreachable (the calls swallow exceptions) and fail
    # the moment real agent_context rows exist.
    stack.enter_context(
        patch("core.costs.enforce_agent_cap", new=AsyncMock())
    )
    stack.enter_context(
        patch("core.agent_context.recent_context_block", new=AsyncMock(return_value=""))
    )
    stack.enter_context(
        patch("core.agent_context.record_context", new=AsyncMock())
    )
    stack.enter_context(
        patch("core.knowledge_graph.link_text_fire_and_forget", new=MagicMock())
    )
    return stack


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


def test_model_tier_resolves():
    assert AgentConfig("ceo", "ceo", "p").model.startswith("claude-")
    assert AgentConfig("cfo", "specialist", "p").model.startswith("claude-")


def test_bad_model_tier_raises():
    with pytest.raises(ValueError):
        _ = AgentConfig("x", "bogus", "p").model


# ---------------------------------------------------------------------------
# run_agent — the loop
# ---------------------------------------------------------------------------


async def test_run_agent_plain_text():
    """No tools requested: one API turn, text returned, usage accumulated."""
    client = _fake_client([_response([_text("hello there")], "end_turn")])
    cfg = AgentConfig("ceo", "ceo", "be the ceo")
    with _agent_env(client):
        result = await run_agent(cfg, "hi")
    assert result.text == "hello there"
    assert result.turns == 1
    assert result.stop_reason == "end_turn"
    assert result.usage["input_tokens"] == 10
    assert result.usage["output_tokens"] == 5
    assert result.tool_calls == []


async def test_run_agent_executes_tool():
    """A tool_use turn runs the handler, then a second turn finishes."""
    handler = AsyncMock(return_value={"ok": True})
    tool = ToolSpec("ping", "ping it", {"type": "object"}, handler)
    client = _fake_client(
        [
            _response([_tool_use("tu_1", "ping", {"x": 1})], "tool_use"),
            _response([_text("done")], "end_turn"),
        ]
    )
    cfg = AgentConfig("ceo", "ceo", "p", tools=[tool])
    with _agent_env(client), patch("core.runtime.policy.gate_for", AsyncMock(return_value=("auto", "ping"))):
        result = await run_agent(cfg, "do it")
    handler.assert_awaited_once_with({"x": 1})
    assert result.turns == 2
    assert result.text == "done"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "ping"
    assert result.tool_calls[0]["is_error"] is False


async def test_run_agent_tool_failure_does_not_crash():
    """A raising handler becomes an error tool_result; the loop continues."""
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    tool = ToolSpec("ping", "d", {"type": "object"}, handler)
    client = _fake_client(
        [
            _response([_tool_use("tu_1", "ping", {})], "tool_use"),
            _response([_text("recovered")], "end_turn"),
        ]
    )
    cfg = AgentConfig("ceo", "ceo", "p", tools=[tool])
    with _agent_env(client), patch("core.runtime.policy.gate_for", AsyncMock(return_value=("auto", "ping"))):
        result = await run_agent(cfg, "do it")
    assert result.tool_calls[0]["is_error"] is True
    assert "boom" in str(result.tool_calls[0]["output"])
    assert result.text == "recovered"


async def test_run_agent_unknown_tool():
    """The model asking for a tool the agent does not have is an error, not a crash."""
    client = _fake_client(
        [
            _response([_tool_use("tu_1", "ghost", {})], "tool_use"),
            _response([_text("ok")], "end_turn"),
        ]
    )
    cfg = AgentConfig("ceo", "ceo", "p")  # no tools
    with _agent_env(client):
        result = await run_agent(cfg, "x")
    assert result.tool_calls[0]["is_error"] is True
    assert "unknown tool" in str(result.tool_calls[0]["output"])


async def test_run_agent_hits_max_turns():
    """A model that never stops asking for tools is capped by max_turns."""
    handler = AsyncMock(return_value="again")
    tool = ToolSpec("loop", "d", {"type": "object"}, handler)
    client = _fake_client([])
    client.messages.create = AsyncMock(
        side_effect=lambda **kw: _response(
            [_tool_use("t", "loop", {})], "tool_use"
        )
    )
    client.beta.messages.create = client.messages.create
    cfg = AgentConfig("ceo", "ceo", "p", tools=[tool], max_turns=3)
    with _agent_env(client):
        result = await run_agent(cfg, "go")
    assert result.turns == 3
    assert result.stop_reason == "max_turns"


# ---------------------------------------------------------------------------
# Approval gates
# ---------------------------------------------------------------------------


def _approval_env(approval_id):
    """No DB, no surface: the row id is faked and notify is a no-op."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("core.runtime.approval.db.fetchval", new=AsyncMock(return_value=approval_id)))
    stack.enter_context(patch("core.runtime.approval.db.execute", new=AsyncMock()))
    stack.enter_context(patch("core.runtime.approval.notify", new=AsyncMock(return_value=None)))
    return stack


async def test_request_approval_approved():
    import asyncio

    aid = uuid4()
    with _approval_env(aid):
        task = asyncio.create_task(ap.request_approval(
            summary="send email", payload={"to": "a@b.com"}, agent_name="cfo",
        ))
        await asyncio.sleep(0.01)
        ap._pending[aid].set_result(("approved", {"to": "a@b.com"}))
        outcome = await task
    assert outcome.approved is True
    assert outcome.payload == {"to": "a@b.com"}
    assert outcome.approval_id == aid


async def test_request_approval_rejected():
    import asyncio

    aid = uuid4()
    with _approval_env(aid):
        task = asyncio.create_task(ap.request_approval(summary="s", payload={}, agent_name="cfo"))
        await asyncio.sleep(0.01)
        ap._pending[aid].set_result(("rejected", None))
        outcome = await task
    assert outcome.approved is False
    assert outcome.status == "rejected"


async def test_execute_on_approval_runs_when_approved():
    executor = AsyncMock(return_value="sent")
    outcome = ap.ApprovalOutcome(status="approved", payload={"to": "x"})
    with patch("core.runtime.approval.db.execute", new=AsyncMock()):
        result = await ap.execute_on_approval(outcome, executor, agent_name="cfo")
    executor.assert_awaited_once_with({"to": "x"})
    assert result == "sent"


async def test_execute_on_approval_skips_when_rejected():
    executor = AsyncMock()
    outcome = ap.ApprovalOutcome(status="rejected", payload=None)
    result = await ap.execute_on_approval(outcome, executor)
    executor.assert_not_awaited()
    assert result is None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_roster_is_registered():
    agents = dp.registered_agents()
    assert "ceo" in agents
    assert "opportunity_scout" in agents
    assert "career" in agents
    assert dp.get_registration("ceo").model_tier == "ceo"
    assert dp.get_registration("opportunity_scout").model_tier == "specialist"


async def test_dispatch_unknown_agent_raises():
    with pytest.raises(ValueError):
        await dp.dispatch("nobody", "task")


async def test_dispatch_runs_registered_agent():
    fake_result = SimpleNamespace(
        episode_id=uuid4(),
        turns=1,
        stop_reason="end_turn",
        usage={"input_tokens": 1},
    )
    cfg = AgentConfig("ceo", "ceo", "p")
    with (
        patch(
            "core.runtime.dispatcher.load_agent_config", return_value=cfg
        ) as load_cfg,
        patch(
            "core.runtime.dispatcher.run_agent",
            new=AsyncMock(return_value=fake_result),
        ) as run,
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()) as execute, \
        patch("core.events.emit", new=AsyncMock()),
    ):
        result = await dp.dispatch("ceo", "plan my week")
    load_cfg.assert_called_once()
    run.assert_awaited_once()
    execute.assert_awaited_once()
    assert result is fake_result



async def test_run_agent_injects_date_preamble():
    """Every run_agent call must prepend 'Today is <date>' to the first
    user message so a dispatched specialist has a wall-clock anchor."""
    from datetime import date

    client = _fake_client([_response([_text("ok")], "end_turn")])
    cfg = AgentConfig("ceo", "ceo", "be the ceo")
    with _agent_env(client):
        await run_agent(cfg, "what does my month look like?")

    sent_messages = client.messages.create.await_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    assert first_content.startswith("Today is ")
    assert date.today().isoformat() in first_content
    assert "what does my month look like?" in first_content


async def test_run_agent_date_preamble_with_memory_context():
    """Memory context still lands between the date preamble and the message."""
    client = _fake_client([_response([_text("ok")], "end_turn")])
    cfg = AgentConfig("cfo", "specialist", "be the cfo")
    with _agent_env(client):
        await run_agent(
            cfg,
            "what's the net?",
            memory_context="known: account=chase_checking",
        )

    sent_messages = client.messages.create.await_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    parts = first_content.split("\n\n---\n\n")
    assert len(parts) == 4
    assert parts[0].startswith("Today is ")
    assert parts[1].startswith("Projects on record (priority order):")
    assert parts[2] == "known: account=chase_checking"
    assert parts[3] == "what's the net?"



async def test_run_agent_raises_when_cap_exceeded():
    """Cost cap is checked before any episode opens or any token is spent."""
    from decimal import Decimal

    from core.costs import CostCapExceeded

    client = _fake_client([_response([_text("never reached")], "end_turn")])
    cfg = AgentConfig("ceo", "ceo", "be the ceo")
    with _agent_env(client), patch(
        "core.runtime.agent.enforce_daily_cap",
        new=AsyncMock(side_effect=CostCapExceeded(Decimal("9.00"), Decimal("8.00"))),
    ):
        with pytest.raises(CostCapExceeded):
            await run_agent(cfg, "any task")
    # The client must NOT have been called: the cap fires before tokens spend.
    client.messages.create.assert_not_called()



async def test_dispatch_caches_within_ttl():
    """A second dispatch with the same agent+task inside the cache TTL
    returns the cached result and does not run the agent again."""
    fake_result = SimpleNamespace(
        episode_id=uuid4(),
        turns=1,
        stop_reason="end_turn",
        usage={"input_tokens": 1},
    )
    cfg = AgentConfig("cfo", "specialist", "p")
    dp.clear_dispatch_cache()
    with (
        patch(
            "core.runtime.dispatcher.load_agent_config", return_value=cfg
        ),
        patch(
            "core.runtime.dispatcher.run_agent",
            new=AsyncMock(return_value=fake_result),
        ) as run,
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()),
    ):
        first = await dp.dispatch("cfo", "show me cash flow")
        second = await dp.dispatch("cfo", "show me cash flow")
    assert first is second
    run.assert_awaited_once()  # only one real run despite two dispatch calls


async def test_dispatch_different_tasks_dont_collide():
    """Two dispatches with different tasks must both run."""
    fake_result = SimpleNamespace(
        episode_id=uuid4(),
        turns=1,
        stop_reason="end_turn",
        usage={"input_tokens": 1},
    )
    cfg = AgentConfig("cfo", "specialist", "p")
    dp.clear_dispatch_cache()
    with (
        patch(
            "core.runtime.dispatcher.load_agent_config", return_value=cfg
        ),
        patch(
            "core.runtime.dispatcher.run_agent",
            new=AsyncMock(return_value=fake_result),
        ) as run,
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()),
    ):
        await dp.dispatch("cfo", "show me cash flow")
        await dp.dispatch("cfo", "list recurring bills")
    assert run.await_count == 2


async def test_dispatch_cache_expires_after_ttl():
    """A dispatch outside the TTL window does run again."""
    fake_result = SimpleNamespace(
        episode_id=uuid4(),
        turns=1,
        stop_reason="end_turn",
        usage={"input_tokens": 1},
    )
    cfg = AgentConfig("cfo", "specialist", "p")
    dp.clear_dispatch_cache()
    with (
        patch(
            "core.runtime.dispatcher.load_agent_config", return_value=cfg
        ),
        patch(
            "core.runtime.dispatcher.run_agent",
            new=AsyncMock(return_value=fake_result),
        ) as run,
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()),
        # Shrink the TTL to 0 so the second call is past it.
        patch("core.runtime.dispatcher._DISPATCH_CACHE_TTL_S", 0.0),
    ):
        await dp.dispatch("cfo", "show me cash flow")
        await dp.dispatch("cfo", "show me cash flow")
    assert run.await_count == 2


async def test_dispatch_explicit_idempotency_key_overrides_default():
    """An explicit key can collapse calls that would otherwise look
    distinct (different tasks) or distinguish ones that would look
    identical (same task, different intent)."""
    fake_result = SimpleNamespace(
        episode_id=uuid4(),
        turns=1,
        stop_reason="end_turn",
        usage={"input_tokens": 1},
    )
    cfg = AgentConfig("cfo", "specialist", "p")
    dp.clear_dispatch_cache()
    with (
        patch(
            "core.runtime.dispatcher.load_agent_config", return_value=cfg
        ),
        patch(
            "core.runtime.dispatcher.run_agent",
            new=AsyncMock(return_value=fake_result),
        ) as run,
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()),
    ):
        # Two different tasks, same explicit key -> cache hit on second.
        await dp.dispatch("cfo", "task one", idempotency_key="shared")
        await dp.dispatch("cfo", "task two", idempotency_key="shared")
    assert run.await_count == 1


# ---------------------------------------------------------------------------
# The projects block is the priority list, injected into the first turn
# ---------------------------------------------------------------------------


async def test_first_turn_carries_projects_block_after_the_date():
    client = _fake_client([_response([_text("ok")], "end_turn")])
    cfg = AgentConfig("ceo", "ceo", "system", tools=[], max_turns=2)
    with _agent_env(client):
        await run_agent(cfg, "brief me")
    first = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert first.startswith("Today is ")
    assert "Projects on record (priority order):\n- demo-dental: Demo Dental [p1 | client | active]" in first
    assert first.endswith("brief me")
    # The system prompt stays a stable, cacheable prefix.
    assert client.messages.create.await_args.kwargs["system"][0]["text"] == "system"


async def test_projects_block_failure_does_not_break_the_run():
    client = _fake_client([_response([_text("ok")], "end_turn")])
    cfg = AgentConfig("ceo", "ceo", "system", tools=[], max_turns=2)
    with _agent_env(client), patch("core.runtime.agent.projects_block", new=AsyncMock(side_effect=RuntimeError("db"))):
        result = await run_agent(cfg, "brief me")
    assert result.text == "ok"
    assert "Projects on record" not in client.messages.create.await_args.kwargs["messages"][0]["content"]
