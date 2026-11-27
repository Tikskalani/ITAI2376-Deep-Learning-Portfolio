"""The event stream (core/events.py) and mission control's routes.

The events table is a private copy (tests/livedb.py); NOTIFY itself is
database-wide, which is the point: a subscriber in this process must hear
an emit from any process. The listener uses its own connection.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, events
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private_tables(monkeypatch):
    from tests.conftest import REAL_EMIT

    monkeypatch.setattr(events, "emit", REAL_EMIT)  # the suite silences the stream; these tests are the stream
    # NOTIFY is database-wide: on the laptop a live dashboard would otherwise
    # watch the test's events go by. The rows are private; so is the channel.
    monkeypatch.setattr(events, "CHANNEL", "atlas_events_test")
    async with livedb.shadow(monkeypatch, "events"):
        yield


@aio
async def test_emit_writes_a_small_row_and_recent_reads_it_back():
    ep = uuid4()
    eid = await events.emit("run.tool", agent="career", episode_id=ep, title="get_posting",
                            detail="x" * 2000, data={"input": {"id": "p1"}, "when": datetime(2026, 10, 11, tzinfo=UTC)})
    assert eid is not None
    rows = await events.recent(limit=10)
    assert [r["id"] for r in rows] == [eid]
    r = rows[0]
    assert r["kind"] == "run.tool" and r["agent"] == "career" and r["episode_id"] == str(ep)
    assert r["detail"].endswith("…") and len(r["detail"]) == 600  # kept small
    assert r["data"] == {"input": {"id": "p1"}, "when": "2026-10-11T00:00:00+00:00"}
    assert json.dumps(r)  # JSON-safe for the wire

    later = await events.emit("notified", title="Hello")
    assert [x["id"] for x in await events.recent(since_id=eid)] == [later]
    assert [x["kind"] for x in await events.recent(kinds=["notified"])] == ["notified"]
    assert await events.recent(start=datetime.now(UTC) + timedelta(minutes=1)) == []


@aio
async def test_a_subscriber_hears_an_emit_through_postgres():
    sub = events.subscribe()
    waiter = asyncio.ensure_future(sub.__anext__())
    await asyncio.sleep(1.5)  # the listener connection comes up in the background
    eid = await events.emit("approval.asked", agent="cfo", title="Pay the bill?", data={"approval_id": "a1"})
    ev = await asyncio.wait_for(waiter, timeout=10)
    assert ev["id"] == eid and ev["kind"] == "approval.asked" and ev["agent"] == "cfo"
    assert ev["data"] == {"approval_id": "a1"} and ev["at"]
    await sub.aclose()


@aio
async def test_prune_keeps_the_window():
    await events.emit("run.started", agent="coo", title="old")
    await db.execute("update events set at = now() - interval '40 days'")
    await events.emit("run.started", agent="coo", title="new")
    assert await events.prune(days=30) == 1
    assert [r["title"] for r in await events.recent()] == ["new"]


@aio
async def test_routes_list_and_stream():
    a = await events.emit("dispatch", agent="ceo", title="asked cfo", detail="Reconcile September")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/events?limit=5")
        assert r.status_code == 200 and [e["id"] for e in r.json()["events"]] == [a]
        assert (await c.get("/api/events?kinds=notified")).json()["events"] == []

    # The wire format (the route streams exactly this): a backfill from
    # `since`, a connected comment, pings while quiet, then live events.
    gen = events.sse(since=a - 1, ping_seconds=0.5)
    frame = await gen.__anext__()
    assert frame.startswith(f"id: {a}\ndata: ") and json.loads(frame.split("data: ", 1)[1])["title"] == "asked cfo"
    assert await gen.__anext__() == ": connected\n\n"
    assert await asyncio.wait_for(gen.__anext__(), timeout=5) == ": ping\n\n"  # quiet: a ping, not a crash
    await asyncio.sleep(1.0)  # the listener connection is up by now
    b = await events.emit("notified", title="live one")
    live = await asyncio.wait_for(gen.__anext__(), timeout=10)
    while live == ": ping\n\n":
        live = await asyncio.wait_for(gen.__anext__(), timeout=10)
    assert live.startswith(f"id: {b}\n") and "live one" in live
    await gen.aclose()


# ------------------------------------------------------------------ the hooks


@pytest_asyncio.fixture(loop_scope="module")
async def _run_tables(monkeypatch):
    async with livedb.shadow(monkeypatch, "episodes", "actions"):
        yield


@aio
async def test_a_run_announces_itself_start_tools_and_finish(_run_tables, monkeypatch):
    from types import SimpleNamespace

    from core.memory import episodic
    from core.runtime import agent as rt

    async def fake_embed(_text):
        return None

    monkeypatch.setattr(episodic, "embed", fake_embed)
    ep = await episodic.start_episode("coo", ["owner", "coo"])

    async def handler(inp):
        return {"found": inp["q"]}

    spec = rt.ToolSpec(name="test_lookup", description="x", input_schema={}, handler=handler)

    async def auto_gate(_name):
        return "auto", "test_lookup"

    monkeypatch.setattr("core.runtime.policy.gate_for", auto_gate)
    cfg = rt.AgentConfig("coo", "specialist", "system", tools=[spec], max_turns=2)
    out, err = await rt._run_tool(spec, SimpleNamespace(name="test_lookup", input={"q": "rent"}), cfg, ep, None)
    assert out == {"found": "rent"} and not err
    out, err = await rt._run_tool(None, SimpleNamespace(name="nope", input={}), cfg, ep, None)
    assert err

    await episodic.end_episode(ep, "coo: checked the rent -> found it", "ok; 1 turn, 2 tool calls",
                               what_changed={"tokens": {"input_tokens": 1000, "output_tokens": 50}, "turns": 1},
                               status="ok")
    kinds = [(e["kind"], e["title"], e["data"].get("ok")) for e in await events.recent(limit=20)
             if e["episode_id"] == str(ep)]
    assert kinds == [
        ("run.started", "starting", None),
        ("run.tool", "test_lookup", None), ("run.tool.done", "test_lookup", True),
        ("run.tool", "nope", None), ("run.tool.done", "nope", False),
        ("run.finished", "coo: checked the rent -> found it", None),
    ]
    finished = [e for e in await events.recent(limit=20) if e["kind"] == "run.finished"][-1]
    assert finished["agent"] == "coo" and finished["data"]["status"] == "ok" and float(finished["data"]["cost"]) > 0
