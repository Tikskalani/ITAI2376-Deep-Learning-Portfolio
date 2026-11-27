"""Tests for the approval policy (core/runtime/policy.py) and its hook in
the agent tool loop (core/runtime/agent._run_tool).

Resolution is pure. The seeded rows from migration 011 are read from the
live test database; rows these tests add use the 'test_' prefix and are
removed. The tool loop is exercised with a fake ToolSpec and a fake
tool_use block, with request_approval replaced so no surface is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from core import db
from core.runtime import agent, policy
from core.runtime.approval import ApprovalOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup():
    policy.invalidate_cache()
    yield
    await db.execute("delete from approval_policy where tool_pattern like 'test_%'")
    await db.execute("delete from actions where agent_name = 'test_policy_agent'")
    await db.execute("delete from tool_ledger where agent_name = 'test_policy_agent'")
    policy.invalidate_cache()


async def test_match_gate_precedence_and_specificity():
    rows = [
        {"tool_pattern": "*send_*", "gate": "approve"},
        {"tool_pattern": "*", "gate": "auto"},
        {"tool_pattern": "gmail_send_*", "gate": "deny"},
        {"tool_pattern": "*transfer*", "gate": "deny"},
    ]
    assert policy.match_gate("recall_facts", rows) == ("auto", "*")
    assert policy.match_gate("send_sms", rows) == ("approve", "*send_*")
    # deny beats approve when both match.
    assert policy.match_gate("gmail_send_message", rows) == ("deny", "gmail_send_*")
    assert policy.match_gate("bank_transfer", rows) == ("deny", "*transfer*")
    assert policy.match_gate("anything", []) == ("unmatched", None)
    assert policy.match_gate("GMAIL_SEND_MESSAGE", rows)[0] == "deny"  # case-insensitive


def test_seeded_policy_fences_the_hard_rules():
    # Migration 011 seeds these, checked with the same matcher the runtime uses.
    # The seed is read from the migration, not the live table: the live table
    # holds his own choices since (2026-10-22: *submit* moved to "on its own").
    import re

    from core.config import REPO_ROOT

    # The hard-rule fences (011) plus every tool's explicit row (037): with
    # fail-closed policy a tool neither matches is refused, not run.
    sql = "".join((REPO_ROOT / "db" / "migrations" / f).read_text(encoding="utf-8")
                  for f in ("011_senses.sql", "037_policy_provenance.sql"))
    rows = [{"tool_pattern": p, "gate": g}
            for p, g in re.findall(r"\('([^']+)',\s*'(auto|approve|deny)'", sql)]
    assert rows, "the migration seeds approval rows"
    assert policy.match_gate("recall_facts", rows)[0] == "auto"
    assert policy.match_gate("gmail_send_message", rows)[0] == "approve"
    assert policy.match_gate("github_comment_on_pr", rows)[0] == "approve"
    assert policy.match_gate("canvas_submit_assignment", rows)[0] == "deny"
    assert policy.match_gate("bank_transfer_funds", rows)[0] == "deny"
    assert policy.match_gate("facebook_publish_listing", rows)[0] == "deny"


async def test_set_and_delete_policy_invalidate_cache():
    await policy.set_policy("test_tool_*", "approve", "for the test")
    assert await policy.gate_for("test_tool_x") == ("approve", "test_tool_*")
    await policy.set_policy("test_tool_*", "deny")
    assert (await policy.gate_for("test_tool_x"))[0] == "deny"
    assert await policy.delete_policy("test_tool_*") is True
    assert (await policy.gate_for("test_tool_x"))[0] == "unmatched"
    with pytest.raises(ValueError):
        await policy.set_policy("test_bad", "maybe")
    with pytest.raises(ValueError):
        await policy.set_policy("   ", "deny")


def _spec(name: str, calls: list) -> agent.ToolSpec:
    async def handler(inp):
        calls.append(inp)
        return {"ok": True}
    return agent.ToolSpec(name=name, description="t", input_schema={}, handler=handler)


def _block(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, input={"to": "x"})


_CONFIG = SimpleNamespace(name="test_policy_agent")


async def _episode() -> object:
    """A real episode row so the actions foreign key is satisfied."""
    return await db.fetchval(
        "insert into episodes (agent_name, summary, started_at) values ('test_policy_agent', 't', now()) returning id"
    )


async def test_run_tool_denies_and_audits_without_calling_handler(monkeypatch):
    calls: list = []
    await policy.set_policy("test_wire_money", "deny")
    ep = await _episode()
    out, is_error = await agent._run_tool(_spec("test_wire_money", calls), _block("test_wire_money"), _CONFIG, ep, None)
    assert is_error and "blocked by Atlas policy" in out and calls == []
    row = await db.fetchrow(
        "select approved_by, inputs, outputs from actions where episode_id = $1 order by created_at desc limit 1", ep
    )
    assert row is not None
    assert row["approved_by"] == "policy"
    assert row["inputs"]["policy"] == "test_wire_money"
    await db.execute("delete from episodes where id = $1", ep)


async def test_run_tool_asks_then_runs_only_on_approval(monkeypatch):
    calls: list = []
    asked: list[dict] = []
    await policy.set_policy("test_send_note", "approve")

    async def fake_request(**kw):
        asked.append(kw)
        return ApprovalOutcome(status=kw["payload"]["input"].get("decide", "rejected"), payload=kw["payload"])

    monkeypatch.setattr("core.runtime.approval.request_approval", fake_request)
    ep = await _episode()

    block = _block("test_send_note")
    out, is_error = await agent._run_tool(_spec("test_send_note", calls), block, _CONFIG, ep, None)
    assert is_error and "not approved (rejected)" in out and calls == []
    assert asked[0]["agent_name"] == "test_policy_agent"
    assert asked[0]["payload"]["tool"] == "test_send_note"

    block.input = {"to": "x", "decide": "approved"}
    out, is_error = await agent._run_tool(_spec("test_send_note", calls), block, _CONFIG, ep, None)
    assert not is_error and out == {"ok": True} and calls == [block.input]
    row = await db.fetchrow(
        "select approved_by from actions where episode_id = $1 order by created_at desc limit 1", ep
    )
    assert row is not None and row["approved_by"] == "user"
    await db.execute("delete from episodes where id = $1", ep)


async def test_run_tool_auto_path_unchanged():
    calls: list = []
    ep = await _episode()
    out, is_error = await agent._run_tool(_spec("recall_facts", calls), _block("recall_facts"), _CONFIG, ep, None)
    assert not is_error and calls == [{"to": "x"}]
    row = await db.fetchrow(
        "select approved_by from actions where episode_id = $1 order by created_at desc limit 1", ep
    )
    assert row is not None and row["approved_by"] == "auto"
    await db.execute("delete from episodes where id = $1", ep)


async def test_gate_for_fails_closed_when_table_unreadable(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    policy.invalidate_cache()
    monkeypatch.setattr(policy.db, "fetch", boom)
    gate, reason = await policy.gate_for("recall_facts")
    assert gate == "unavailable" and "db down" in (reason or "")
    policy.invalidate_cache()


def _capture_events(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    async def emit(kind, **kw):
        seen.append({"kind": kind, **kw})

    monkeypatch.setattr("core.events.emit", emit)
    return seen


async def test_run_tool_refuses_an_unmatched_tool_and_says_what_to_fix(monkeypatch):
    calls: list = []
    seen = _capture_events(monkeypatch)
    ep = await _episode()
    out, is_error = await agent._run_tool(_spec("test_brand_new_tool", calls), _block("test_brand_new_tool"),
                                          _CONFIG, ep, None)
    assert is_error and "has no Atlas policy" in out and calls == []
    row = await db.fetchrow(
        "select approved_by, inputs, outputs from actions where episode_id = $1 order by created_at desc limit 1", ep
    )
    assert row["approved_by"] == "policy" and row["inputs"]["gate"] == "unmatched"
    refused = [e for e in seen if e["kind"] == "policy.refused"]
    assert len(refused) == 1 and "test_brand_new_tool" in refused[0]["title"]
    assert "Trust" in refused[0]["detail"] and refused[0]["episode_id"] == ep
    await db.execute("delete from episodes where id = $1", ep)


async def test_run_tool_refuses_when_the_policy_cannot_be_read(monkeypatch):
    calls: list = []
    seen = _capture_events(monkeypatch)
    ep = await _episode()

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(policy, "list_policies", boom)
    # recall_facts is auto when the table is readable; unreadable refuses it too.
    out, is_error = await agent._run_tool(_spec("recall_facts", calls), _block("recall_facts"), _CONFIG, ep, None)
    assert is_error and "could not read its tool policy" in out and calls == []
    refused = [e for e in seen if e["kind"] == "policy.refused"]
    assert len(refused) == 1 and "unreadable" in refused[0]["title"] and "db down" in refused[0]["detail"]
    await db.execute("delete from episodes where id = $1", ep)


async def test_server_tools_attach_only_when_the_policy_says_auto():
    cfg = agent.AgentConfig(name="test_policy_agent", model_tier="specialist", system_prompt="s",
                            server_tools=["web_search", "web_fetch", "test_mystery"])
    out = await agent._policed_server_tools(cfg)
    # Seeded auto for the two real ones; an unknown server tool has no row.
    assert out.server_tools == ["web_search", "web_fetch"]
    assert cfg.server_tools == ["web_search", "web_fetch", "test_mystery"]  # the original is untouched

    await policy.set_policy("server:web_fetch", "approve")
    try:
        # Atlas never sees a server-tool call, so "ask first" cannot be kept: not attached.
        out = await agent._policed_server_tools(cfg)
        assert out.server_tools == ["web_search"]
    finally:
        await policy.set_policy("server:web_fetch", "auto")
