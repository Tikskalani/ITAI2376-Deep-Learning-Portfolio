"""Late approvals finish the job (core/runtime/late.py).

A gated tool call gives up waiting after ATLAS_APPROVAL_WAIT_MIN and leaves
its card open. These tests run the real gate with a zero wait, decide the
card afterwards the way the PWA does, and check that a late yes runs the
very call once, a late no closes it, and the scheduler's sweep catches what
a web process left behind. Every table touched is a private copy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core import db, events
from core.config import settings
from core.runtime import agent, dispatcher, late, ledger, policy
from core.runtime import approval as ap
from tests import livedb

AGENT = "test_late_agent"
_CONFIG = SimpleNamespace(name=AGENT)
aio = pytest.mark.asyncio(loop_scope="module")

TABLES = ("approvals", "tool_ledger", "actions", "notifications", "approval_policy",
          "episodes", "events", "push_subscriptions")


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    from tests.conftest import REAL_EMIT

    async with livedb.shadow(monkeypatch, *TABLES):
        monkeypatch.setattr(events, "emit", REAL_EMIT)  # the suite silences the stream; here it is evidence
        monkeypatch.setattr(events, "CHANNEL", "atlas_events_test")
        monkeypatch.setattr(settings, "approval_wait_minutes", 0)   # give up at once
        policy.invalidate_cache()
        await policy.set_policy("test_late_*", "approve")
        calls: list[dict] = []
        tools: list[agent.ToolSpec] = []
        dispatcher.register_agent(AGENT, "specialist", tools_factory=lambda: list(tools))
        ep = await db.fetchval(
            "insert into episodes (agent_name, summary, started_at) values ($1, 't', now()) returning id", AGENT
        )
        try:
            yield SimpleNamespace(calls=calls, tools=tools, episode=ep)
        finally:
            await late.drain()
            dispatcher._REGISTRY.pop(AGENT, None)
            ap._pending.clear()
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


async def _give_up(world, spec, **inp):
    """Run the gated call through the real loop; it waits zero seconds and
    reports the card as still open. Returns the approval id."""
    out, err = await agent._run_tool(spec, _block(spec.name, **inp), _CONFIG, world.episode, None)
    assert err and "still open" in out
    row = await db.fetchrow("select approval_id, status from tool_ledger where tool = $1 order by created_at desc limit 1",
                            spec.name)
    assert row["status"] == "pending" and row["approval_id"] is not None
    return row["approval_id"]


async def _ledger(tool: str) -> dict:
    return dict(await db.fetchrow("select status, result, attempts from tool_ledger where tool = $1 order by created_at desc limit 1", tool))


# ------------------------------------------------------------------ a late yes


@aio
async def test_a_late_yes_runs_the_call_once_and_tells_him(world):
    spec = _spec("test_late_send", world.calls)
    world.tools.append(spec)
    with ledger.ensure_scope("dispatch:late-1"):
        aid = await _give_up(world, spec, to="a@b.com", body="hi")
    assert world.calls == []                                   # the agent gave up without sending

    row = await ap.decide_approval(aid, "approved", decided_by="owner/pwa")
    assert row and row["status"] == "approved"
    await late.drain()

    assert world.calls == [{"to": "a@b.com", "body": "hi"}]   # sent exactly once
    led = await _ledger("test_late_send")
    assert led["status"] == "done" and led["result"] == {"sent": "a@b.com"} and led["attempts"] == 2
    audit = await db.fetchrow(
        "select inputs, outputs, approved_by, episode_id from actions where agent_name = $1 "
        "and (inputs->>'late')::bool order by created_at desc limit 1", AGENT)
    assert audit["approved_by"] == "user" and audit["episode_id"] == world.episode
    assert audit["inputs"]["tool"] == "test_late_send" and audit["outputs"]["result"] == {"sent": "a@b.com"}
    note = await db.fetchrow("select title, body, kind, data from notifications order by created_at desc limit 1")
    assert note["kind"] == "approval" and note["title"] == "Done on your yes: test_late_send"
    assert "ran after you approved it" in note["body"] and note["data"]["finished"] is True
    ev = await db.fetchrow("select agent, title, data from events where kind = 'approval.finished' order by id desc limit 1")
    assert ev["agent"] == AGENT and ev["data"]["ok"] is True and ev["data"]["approval_id"] == str(aid)

    # Finishing again does nothing: the ledger row is no longer pending.
    assert await late.finish(aid) is None and len(world.calls) == 1
    # And a retry of the same call inside the same unit of work gets the record.
    with ledger.ensure_scope("dispatch:late-1"):
        out, err = await agent._run_tool(spec, _block(spec.name, to="a@b.com", body="hi"), _CONFIG, world.episode, None)
    assert not err and out["result"] == {"sent": "a@b.com"} and len(world.calls) == 1


@aio
async def test_a_late_no_closes_the_call_for_its_unit_of_work(world):
    spec = _spec("test_late_post", world.calls)
    world.tools.append(spec)
    with ledger.ensure_scope("dispatch:late-2"):
        aid = await _give_up(world, spec, text="hello")
        await ap.decide_approval(aid, "rejected")
        await late.drain()
        assert (await _ledger("test_late_post"))["status"] == "rejected" and world.calls == []
        out, err = await agent._run_tool(spec, _block(spec.name, text="hello"), _CONFIG, world.episode, None)
    assert err and "already rejected" in out and world.calls == []


# ------------------------------------------------------------------ the sweep


@aio
async def test_the_sweep_finishes_what_a_web_process_left_behind(world):
    spec = _spec("test_late_sweep", world.calls)
    world.tools.append(spec)
    with ledger.ensure_scope("dispatch:late-3"):
        approved = await _give_up(world, spec, to="x@y.z")
        expired = await _give_up(world, spec, to="gone@y.z")
    # Decided without decide_approval: the process that would have finished it is gone.
    await db.execute("update approvals set status = 'approved', resolved_at = now(), decided_by = 'owner/pwa' where id = $1", approved)
    await db.execute("update approvals set status = 'expired', resolved_at = now() where id = $1", expired)

    assert await late.sweep() == 1
    assert world.calls == [{"to": "x@y.z"}]
    statuses = {r["input"]["to"]: r["status"] for r in await db.fetch("select input, status from tool_ledger where tool = 'test_late_sweep'")}
    assert statuses == {"x@y.z": "done", "gone@y.z": "expired"}
    assert await late.sweep() == 0 and len(world.calls) == 1


# ------------------------------------------------------------------ when it cannot


@aio
async def test_a_missing_tool_runs_nothing_and_says_so(world):
    spec = _spec("test_late_vanished", world.calls)
    aid = await _give_up(world, spec, to="a@b.com")          # not in the agent's tools any more
    await ap.decide_approval(aid, "approved")
    await late.drain()
    assert world.calls == [] and (await _ledger("test_late_vanished"))["status"] == "failed"
    note = await db.fetchrow("select title, body, data from notifications order by created_at desc limit 1")
    assert note["title"] == "Could not finish: test_late_vanished" and "nothing ran" in note["body"]
    assert note["data"]["finished"] is False


@aio
async def test_a_crash_counts_as_maybe_sent_unless_the_tool_never_started(world):
    boom = _spec("test_late_boom", world.calls, fail=RuntimeError("smtp timeout"))
    clean = _spec("test_late_clean", world.calls, fail=ledger.NotStarted("bad address"))
    world.tools.extend([boom, clean])
    a1 = await _give_up(world, boom, to="a@b.com")
    a2 = await _give_up(world, clean, to="nobody")
    await ap.decide_approval(a1, "approved")
    await ap.decide_approval(a2, "approved")
    await late.drain()
    assert (await _ledger("test_late_boom"))["status"] == "running"       # may have acted: not retried
    assert (await _ledger("test_late_clean"))["status"] == "failed"       # never acted: may be tried again
    notes = {r["title"]: r["body"] for r in await db.fetch("select title, body from notifications")}
    assert "smtp timeout" in notes["Could not finish: test_late_boom"]
    assert "can be tried again" in notes["Could not finish: test_late_clean"]


@aio
async def test_a_decision_while_the_agent_still_waits_is_not_finished_twice(world):
    """The requester is awake: it runs the call itself and late.finish finds nothing."""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "approval_wait_minutes", 1)
    spec = _spec("test_late_awake", world.calls)
    world.tools.append(spec)

    async def approve_soon():
        for _ in range(200):
            await asyncio.sleep(0.01)
            row = await db.fetchrow("select id from approvals where status = 'pending' and action_summary like '%test_late_awake%'")
            if row:
                return await ap.decide_approval(row["id"], "approved")
        raise AssertionError("no card appeared")

    try:
        run = asyncio.ensure_future(agent._run_tool(spec, _block(spec.name, to="live@b.com"), _CONFIG, world.episode, None))
        decided = await approve_soon()
        out, err = await run
    finally:
        monkey.undo()
    assert not err and out == {"sent": "live@b.com"} and world.calls == [{"to": "live@b.com"}]
    await late.drain()
    assert await late.finish(decided["id"]) is None and len(world.calls) == 1


# ------------------------------------------------------- the policy moved on


@aio
async def test_a_late_yes_does_not_run_a_tool_the_policy_now_refuses(world):
    # The card was raised under "approve". By the time he says yes the tool is
    # denied; an old yes must not outrank the current policy.
    spec = _spec("test_late_wire", world.calls)
    world.tools.append(spec)
    await policy.set_policy("test_late_wire", "approve")
    aid = await _give_up(world, spec, to="x")
    await policy.set_policy("test_late_wire", "deny")

    await ap.decide_approval(aid, "approved", decided_by="owner/pwa")
    await late.drain()

    assert world.calls == []
    led = await _ledger("test_late_wire")
    assert led["status"] == "failed" and led["result"] == {"error": "policy deny"}
    note = await db.fetchrow("select title, body from notifications order by created_at desc limit 1")
    assert "did not run" in note["body"] and "blocked by Atlas policy" in note["body"]
