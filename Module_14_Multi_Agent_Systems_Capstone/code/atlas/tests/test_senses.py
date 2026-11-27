"""Tests for the senses cycle (core/loop/senses.py) and its place in the
executive tick. Connectors, triage, dispatch and delivery are replaced
with fakes; signals, projects and the actions audit are the live test DB."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from core import db, projects, signals
from core.loop import executive, senses
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Routing and triage work on every signal and the interrupt budget counts
    # every senses action; real ones must be neither routed, triaged nor counted,
    # and the real loop heartbeat must not look fresh because a test ticked.
    async with livedb.shadow(monkeypatch, "signals", "actions", "projects", "deadlines", "llm_calls",
                             "system_heartbeats"):
        yield


# Routing orders by occurred_at. Windows' clock can hand two signals made in
# a row the same instant, so each test signal gets a strictly later one.
_tick = itertools.count()


def _sig(ext: str, **kw) -> signals.Signal:
    base = dict(source="test-senses", external_id=ext,
                occurred_at=datetime.now(UTC) + timedelta(milliseconds=next(_tick)),
                subject=f"subject {ext}", body="details")
    base.update(kw)
    return signals.Signal(**base)


async def _triaged(ext: str, urgency: int, project_id=None, **kw):
    sid = await signals.insert_signal(_sig(ext, project_id=project_id, **kw))
    assert sid
    await signals.set_triage(sid, urgency=urgency, importance=urgency, notes=f"gist {ext}")
    return sid


class _FakeConnector:
    def __init__(self, name: str, result=None, boom: bool = False) -> None:
        self.name, self.result, self.boom = name, result, boom
        self.calls = 0

    async def sync(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("down")
        return self.result


async def test_sync_connectors_guards_each_connector(monkeypatch):
    ok = _FakeConnector("github", {"new_signals": 2})
    bad = _FakeConnector("canvas", boom=True)
    monkeypatch.setattr(senses, "connectors", lambda: [ok, bad])
    out = await senses.sync_connectors()
    assert out["github"] == {"new_signals": 2}
    assert out["canvas"]["error"].startswith("RuntimeError")
    assert ok.calls == 1 and bad.calls == 1


async def test_route_interrupts_within_budget_prepares_and_holds(monkeypatch):
    proj = await projects.upsert_project("test-hvac", "Demo HVAC", owner_agent="client_delivery")
    u3a = await _triaged("u3a", 3, proj.id, url="https://x/1", deadline=date(2026, 10, 8))
    u3b = await _triaged("u3b", 3)
    u3c = await _triaged("u3c", 3)
    u2 = await _triaged("u2", 2, proj.id)
    u1 = await _triaged("u1", 1, proj.id)

    delivered: list[str] = []
    dispatched: list[tuple] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    async def fake_dispatch(agent, task, **kw):
        dispatched.append((agent, task, kw))

    monkeypatch.setattr(senses, "dispatch", fake_dispatch)
    monkeypatch.setattr(senses.settings, "interrupt_budget", 2)

    out = await senses.route_signals(deliver)
    assert out == {"interrupted": 2, "held_over_budget": 1, "prepared": 1}
    assert delivered[0] == "[Demo HVAC] gist u3a\nDue 2026-10-08.\nhttps://x/1"
    assert delivered[1] == "gist u3b"
    assert await senses.interrupts_today() == 2

    # The specialist got the project's owner agent and a no-send instruction.
    agent, task, kw = dispatched[0]
    assert agent == "client_delivery"
    assert "gist u2" in task and "Do not send, post, or submit" in task
    assert kw["dispatched_by"] == "loop/senses"

    status = {r["external_id"]: r["triage_status"]
              for r in await signals.list_signals(None, limit=100) if r["source"] == "test-senses"}
    assert status["u3a"] == "actioned" and status["u3b"] == "actioned"
    assert status["u3c"] == "triaged"   # held: budget spent, the brief will carry it
    assert status["u2"] == "actioned"
    assert status["u1"] == "triaged"    # holds for the brief
    assert u3a and u3b and u3c and u2 and u1

    # Budget persists across ticks: the third urgent signal still waits.
    out = await senses.route_signals(deliver)
    assert out["interrupted"] == 0 and out["held_over_budget"] == 1


async def test_route_survives_delivery_and_dispatch_failures(monkeypatch):
    await _triaged("boom3", 3)
    await _triaged("boom2", 2)

    async def deliver(text: str) -> None:
        raise RuntimeError("push down")

    async def fake_dispatch(agent, task, **kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(senses, "dispatch", fake_dispatch)
    monkeypatch.setattr(senses.settings, "interrupt_budget", 5)
    out = await senses.route_signals(deliver)
    assert out == {"interrupted": 0, "held_over_budget": 0, "prepared": 0}
    status = {r["external_id"]: r["triage_status"]
              for r in await signals.list_signals("triaged", limit=100) if r["source"] == "test-senses"}
    # Neither was marked actioned, so the next tick retries both.
    assert status["boom3"] == "triaged" and status["boom2"] == "triaged"
    assert await senses.interrupts_today() == 0


async def test_run_senses_cycle_reports_each_step(monkeypatch):
    async def fake_sync():
        return {"github": {"new_signals": 0}}

    async def fake_triage(batch=None):
        return {"triaged": 4}

    async def fake_route(deliver):
        raise RuntimeError("route exploded")

    monkeypatch.setattr(senses, "sync_connectors", fake_sync)
    monkeypatch.setattr(senses, "triage_new_signals", fake_triage)
    monkeypatch.setattr(senses, "route_signals", fake_route)

    async def deliver(text: str) -> None:
        pass

    out = await senses.run_senses_cycle(deliver)
    assert out["sync"] == {"github": {"new_signals": 0}}
    assert out["triage"] == {"triaged": 4}
    assert out["route"] == {"error": "route exploded"}


async def test_executive_cycle_includes_senses_and_never_raises(monkeypatch):
    seen: dict = {}

    async def fake_senses(deliver):
        seen["deliver"] = deliver
        return {"triage": {"triaged": 2}, "route": {"interrupted": 1, "prepared": 1}}

    async def no_triggers():
        return 0

    async def no_ping(url=None):
        return False

    monkeypatch.setattr(executive, "run_senses_cycle", fake_senses)
    monkeypatch.setattr(executive, "_fire_due_triggers", no_triggers)
    monkeypatch.setattr(executive, "ping_external_heartbeat", no_ping)
    out = await executive.run_executive_cycle()
    assert out["signals_triaged"] == 2 and out["interrupts"] == 1 and out["prepared"] == 1
    assert seen["deliver"] is executive._deliver_interrupt
    hb = await db.fetchrow("select metadata from system_heartbeats where kind = 'loop/executive'")
    assert hb is not None and hb["metadata"]["interrupts"] == 1
