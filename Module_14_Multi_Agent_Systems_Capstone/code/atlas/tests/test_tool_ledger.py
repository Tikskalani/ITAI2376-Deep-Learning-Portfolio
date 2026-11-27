"""The tool ledger (core/runtime/ledger.py): a gated action never happens
twice inside one unit of work, through core/runtime/agent._run_tool.

Uses the live test database with a 'test_ledger_*' approve policy and a
fake handler that counts calls; request_approval is replaced so no card or
push is sent. Every row these tests write is removed.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from core import db
from core.runtime import agent, ledger, policy
from core.runtime.approval import ApprovalOutcome

AGENT = "test_ledger_agent"
_CONFIG = SimpleNamespace(name=AGENT)
aio = pytest.mark.asyncio(loop_scope="module")


# ------------------------------------------------------------------ pure


def test_call_key_ignores_key_order_and_separates_scope_and_tool():
    a = ledger.call_key("s1", "send", {"to": "x", "body": "hi"})
    assert a == ledger.call_key("s1", "send", {"body": "hi", "to": "x"})
    assert a != ledger.call_key("s2", "send", {"to": "x", "body": "hi"})
    assert a != ledger.call_key("s1", "post", {"to": "x", "body": "hi"})
    assert a != ledger.call_key("s1", "send", {"to": "y", "body": "hi"})


def test_outer_scope_wins_and_scopes_unwind():
    assert ledger.current_scope() is None
    with ledger.ensure_scope("workflow:1:step:2") as outer:
        with ledger.ensure_scope("dispatch:x") as inner:
            assert inner == outer == ledger.current_scope() == "workflow:1:step:2"
        assert ledger.current_scope() == "workflow:1:step:2"
    assert ledger.current_scope() is None
    with ledger.ensure_scope("dispatch:y"):
        assert ledger.current_scope() == "dispatch:y"


def test_messages_name_the_tool():
    assert "not repeated" in ledger.Prior("done", None).message("send_x")
    assert "may have run" in ledger.Prior("running", None).message("send_x")
    assert "already open" in ledger.Prior("pending", None).message("send_x")
    assert "already rejected" in ledger.Prior("rejected", None).message("send_x")


# ------------------------------------------------------------------ through the tool loop


@pytest_asyncio.fixture(loop_scope="module")
async def gated(monkeypatch):
    policy.invalidate_cache()
    await policy.set_policy("test_ledger_*", "approve")
    asked: list[dict] = []
    decision = {"status": "approved"}

    async def fake_request(**kw):
        asked.append(kw)
        return ApprovalOutcome(status=decision["status"], payload=kw["payload"], approval_id=uuid4())

    monkeypatch.setattr("core.runtime.approval.request_approval", fake_request)
    ep = await db.fetchval(
        "insert into episodes (agent_name, summary, started_at) values ($1, 't', now()) returning id", AGENT
    )
    yield SimpleNamespace(asked=asked, decision=decision, episode=ep)
    await db.execute("delete from tool_ledger where agent_name = $1", AGENT)
    await db.execute("delete from actions where agent_name = $1", AGENT)
    await db.execute("delete from episodes where id = $1", ep)
    await db.execute("delete from approval_policy where tool_pattern like 'test_ledger_%'")
    policy.invalidate_cache()


def _spec(name: str, calls: list, fail: Exception | None = None) -> agent.ToolSpec:
    async def handler(inp):
        calls.append(inp)
        if fail is not None:
            raise fail
        return {"sent": inp.get("to")}
    return agent.ToolSpec(name=name, description="t", input_schema={}, handler=handler)


def _block(name: str, **inp) -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inp or {"to": "a@b.com"})


async def _run(spec, block, ep):
    return await agent._run_tool(spec, block, _CONFIG, ep, None)


@aio
async def test_a_retry_in_the_same_scope_returns_the_record_not_a_second_send(gated):
    calls: list = []
    spec = _spec("test_ledger_send", calls)
    with ledger.ensure_scope(f"workflow:{uuid4()}:step:1"):
        out, err = await _run(spec, _block("test_ledger_send"), gated.episode)
        assert not err and out == {"sent": "a@b.com"}
        out2, err2 = await _run(spec, _block("test_ledger_send"), gated.episode)
    assert calls == [{"to": "a@b.com"}]          # acted once
    assert len(gated.asked) == 1                  # asked once
    assert not err2 and out2["result"] == {"sent": "a@b.com"} and "not repeated" in out2["note"]
    audit = await db.fetch(
        "select outputs from actions where agent_name = $1 order by created_at", AGENT
    )
    assert audit[-1]["outputs"] == {"ledger": "done"}


@aio
async def test_a_new_unit_of_work_may_do_it_again(gated):
    calls: list = []
    spec = _spec("test_ledger_again", calls)
    for _ in range(2):
        with ledger.ensure_scope(f"dispatch:{uuid4()}"):
            out, err = await _run(spec, _block("test_ledger_again"), gated.episode)
            assert not err
    assert len(calls) == 2 and len(gated.asked) == 2


@aio
async def test_different_input_is_a_different_action(gated):
    calls: list = []
    spec = _spec("test_ledger_input", calls)
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        await _run(spec, _block("test_ledger_input", to="a@b.com"), gated.episode)
        await _run(spec, _block("test_ledger_input", to="c@d.com"), gated.episode)
    assert [c["to"] for c in calls] == ["a@b.com", "c@d.com"]


@aio
async def test_rejected_is_not_asked_again_and_pending_raises_no_second_card(gated):
    calls: list = []
    gated.decision["status"] = "rejected"
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        out, err = await _run(_spec("test_ledger_reject", calls), _block("test_ledger_reject"), gated.episode)
        assert err and "not approved (rejected)" in out
        out, err = await _run(_spec("test_ledger_reject", calls), _block("test_ledger_reject"), gated.episode)
        assert err and "already rejected" in out
    assert len(gated.asked) == 1 and calls == []

    gated.decision["status"] = "pending"
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        out, err = await _run(_spec("test_ledger_wait", calls), _block("test_ledger_wait"), gated.episode)
        assert err and "still open" in out
        out, err = await _run(_spec("test_ledger_wait", calls), _block("test_ledger_wait"), gated.episode)
        assert err and "already open" in out
    assert len(gated.asked) == 2 and calls == []


@aio
async def test_an_expired_card_may_be_asked_again(gated):
    calls: list = []
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        gated.decision["status"] = "expired"
        out, err = await _run(_spec("test_ledger_exp", calls), _block("test_ledger_exp"), gated.episode)
        assert err and calls == []
        gated.decision["status"] = "approved"
        out, err = await _run(_spec("test_ledger_exp", calls), _block("test_ledger_exp"), gated.episode)
        assert not err and len(calls) == 1
    row = await db.fetchrow("select status, attempts from tool_ledger where tool = 'test_ledger_exp'")
    assert row["status"] == "done" and row["attempts"] == 2


@aio
async def test_a_crash_counts_as_maybe_sent_unless_the_tool_says_it_never_started(gated):
    calls: list = []
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        out, err = await _run(_spec("test_ledger_crash", calls, TimeoutError("socket")),
                              _block("test_ledger_crash"), gated.episode)
        assert err and "TimeoutError" in out
        out, err = await _run(_spec("test_ledger_crash", calls), _block("test_ledger_crash"), gated.episode)
        assert err and "may have run" in out
    assert len(calls) == 1  # the retry did not act

    calls.clear()
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        await _run(_spec("test_ledger_safe", calls, ledger.NotStarted("bad address")),
                   _block("test_ledger_safe"), gated.episode)
        out, err = await _run(_spec("test_ledger_safe", calls), _block("test_ledger_safe"), gated.episode)
        assert not err and out == {"sent": "a@b.com"}
    assert len(calls) == 2


@aio
async def test_ungated_tools_never_touch_the_ledger(gated):
    calls: list = []
    with ledger.ensure_scope(f"dispatch:{uuid4()}"):
        for _ in range(2):
            out, err = await _run(_spec("recall_facts", calls), _block("recall_facts"), gated.episode)
            assert not err
    assert len(calls) == 2
    assert await db.fetchval("select count(*) from tool_ledger where tool = 'recall_facts'") == 0


@aio
async def test_without_a_scope_the_episode_is_the_unit(gated):
    calls: list = []
    spec = _spec("test_ledger_episode", calls)
    await _run(spec, _block("test_ledger_episode"), gated.episode)
    out, err = await _run(spec, _block("test_ledger_episode"), gated.episode)
    assert not err and "not repeated" in out["note"] and len(calls) == 1
    row = await db.fetchrow("select scope from tool_ledger where tool = 'test_ledger_episode'")
    assert row["scope"] == f"episode:{gated.episode}"


# ------------------------------------------------------------------ who sets the scope


async def _noop(*a, **k):
    return None


@aio
async def test_a_workflow_step_owns_the_scope_for_all_its_attempts(monkeypatch):
    from core.workflow_engine import _execute_step

    seen: list[str | None] = []

    async def recording_dispatch(agent_name, task, **kw):
        seen.append(ledger.current_scope())
        return SimpleNamespace(text="done")

    monkeypatch.setattr("core.workflow_engine._set_step", _noop)
    monkeypatch.setattr("core.workflow_engine._set_run", _noop)
    run = {"id": uuid4(), "name": "test"}
    step = {"id": uuid4(), "index": 0, "type": "agent", "status": "pending", "attempts": 0,
            "input": {"agent": "x", "task": "t"}}
    await _execute_step(run, step, {}, recording_dispatch, None)
    await _execute_step(run, {**step, "attempts": 1}, {}, recording_dispatch, None)  # a retry
    assert seen == [f"workflow:{run['id']}:step:{step['id']}"] * 2
    assert ledger.current_scope() is None


@aio
async def test_dispatch_gives_both_its_attempts_one_scope_unless_a_step_already_did(monkeypatch):
    from core.runtime import dispatcher

    seen: list[str | None] = []

    async def weak_then_ok(config, task, **kw):
        seen.append(ledger.current_scope())
        # The first attempt comes back weak, so dispatch retries once.
        return SimpleNamespace(text="" if len(seen) % 2 else "ok", stop_reason="end_turn",
                               episode_id=uuid4(), turns=1, usage={})

    monkeypatch.setattr(dispatcher, "run_agent", weak_then_ok)
    monkeypatch.setattr(dispatcher, "_REGISTRY", {"x": SimpleNamespace(
        name="x", model_tier="haiku", tools_factory=None, overrides={})})
    monkeypatch.setattr(dispatcher, "load_agent_config", lambda **kw: SimpleNamespace(name="x"))
    monkeypatch.setattr(dispatcher, "db", SimpleNamespace(execute=_noop))
    dispatcher.clear_dispatch_cache()

    await dispatcher.dispatch("x", "task one")
    assert len(seen) == 2 and seen[0] == seen[1] and seen[0].startswith("dispatch:x:")

    dispatcher.clear_dispatch_cache()
    with ledger.ensure_scope("workflow:w:step:s"):
        await dispatcher.dispatch("x", "task two")
    assert seen[2:] == ["workflow:w:step:s", "workflow:w:step:s"]
    dispatcher.clear_dispatch_cache()
