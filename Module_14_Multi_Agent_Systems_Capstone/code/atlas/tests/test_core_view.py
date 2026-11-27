"""The Today room (core/core_view.py, /api/core/*): shapes, privacy, decisions.

Fixtures only (.test domains) on shadowed tables; no model call, no push.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from core import core_view, db, privacy
from core.config import settings
from core.web.api import app
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")
TABLES = ("events", "approvals", "night_tasks", "slo_checks", "system_heartbeats", "episodes", "llm_calls",
          "tool_ledger", "notifications")
SECRET = "SECRETSUBJECT root canal"
BODY = "SECRETBODY Jane Roe DOB 01/02/1970"

RULES = """
rules:
  - label: dental clients
    domains: [client-dental.test]
content_screen: true
mail_model_reads: {reads}
"""


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    from core.runtime.register_all import register_all

    register_all()


@pytest.fixture
def rules(tmp_path, monkeypatch):
    path = tmp_path / "model_exclude.yaml"

    def use(reads: bool = False) -> None:
        path.write_text(RULES.format(reads=str(reads).lower()), encoding="utf-8")
        monkeypatch.setattr(privacy, "RULES_PATH", path)
        monkeypatch.setattr(settings, "mail_model_reads", reads)
        privacy.reset_cache()

    use()
    return use


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


@pytest.fixture
def quiet(monkeypatch):
    """No late runs and no health probe: the decision tests are about the approval rows."""
    from core.runtime import late

    async def _settle(_id: Any) -> None:
        return None

    monkeypatch.setattr(late, "start", lambda _id: None)
    monkeypatch.setattr(late, "settle_rejected", _settle)

    async def _ok() -> dict[str, Any]:
        return {"status": "ok", "problems": [], "warnings": []}

    monkeypatch.setattr(core_view, "_health_status", _ok)
    quiet_sources(monkeypatch)


def quiet_sources(monkeypatch, *, overdue: int = 0, ideas: int = 0, setup_left: int = 0) -> None:
    """The attention sources, faked: no planner read, no review queue, no browser preflight."""
    from core import discovery, planner, setup_checklist

    async def agenda(**_: Any) -> dict[str, Any]:
        return {"counts": {"overdue": overdue}}

    async def queue(limit: int = 20) -> list[dict[str, Any]]:
        return [{"id": str(i)} for i in range(ideas)]

    async def checklist() -> dict[str, Any]:
        return {"total": 5, "done": 5 - setup_left, "items": []}

    monkeypatch.setattr(planner, "agenda", agenda)
    monkeypatch.setattr(discovery, "list_review_queue", queue)
    monkeypatch.setattr(setup_checklist, "checklist", checklist)


async def _approval(tool: str, inp: dict[str, Any], *, agent: str = "client_delivery",
                    expires: datetime | None = None) -> UUID:
    return await db.fetchval(
        "insert into approvals (agent_name, action_summary, action_payload, expires_at) "
        "values ($1, $2, $3, $4) returning id",
        agent, f"{agent} wants to run {tool}", {"tool": tool, "input": inp},
        expires or datetime.now(UTC) + timedelta(hours=4))


def _blob(x: Any) -> str:
    return json.dumps(x, default=str)


# ------------------------------------------------------------------ shapes


async def test_state_has_the_contract_shape(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        r = await client.get("/api/core/state")
    assert r.status_code == 200
    s = r.json()
    assert s["demo"] is False
    assert {"agents", "decisions", "health", "overnight", "roster", "recent"} <= set(s)
    ids = [a["id"] for a in s["roster"]]
    assert ids[:3] == ["coo", "cto", "career"], "ring order comes from config/core_view.yaml"
    for a in s["agents"]:
        assert set(a) == {"id", "name", "state", "last_action", "last_at"}
        assert a["state"] in {"active", "idle", "waiting", "sending", "receiving", "error"}
        assert len(a["last_action"]) <= 140
    h = s["health"]
    assert h["status"] == "normal" and h["spend_cap"] == float(settings.daily_usd_cap)
    assert h["night_cap"] == float(settings.night_usd_cap) and isinstance(h["briefs_on_time_7d"], list)
    assert s["overnight"] == {"done": 0, "spend": 0.0}
    assert s["decisions"] == [] and s["recent"] == []


async def test_a_warning_or_an_unread_alert_is_not_all_clear(client, rules, quiet, monkeypatch):
    async def warned() -> dict[str, Any]:
        return {"status": "ok", "problems": [], "warnings": ["No phone is subscribed to push."]}

    async with livedb.shadow(monkeypatch, *TABLES):
        clean = (await client.get("/api/core/state")).json()["health"]
        monkeypatch.setattr(core_view, "_health_status", warned)
        warned_h = (await client.get("/api/core/state")).json()["health"]
        monkeypatch.setattr(core_view, "_health_status", quiet_ok)
        for _ in range(4):
            await db.execute("insert into notifications (kind, title, priority) values ('health', 'Atlas loop stalled', 'high')")
        await db.execute("insert into notifications (kind, title, priority) values ('brief', 'Morning brief', 'normal')")
        await db.execute("insert into notifications (kind, title, priority, read_at) "
                         "values ('health', 'Old outage', 'high', now())")
        alerted = (await client.get("/api/core/state")).json()
    assert clean["status"] == "normal" and clean["reasons"] == []
    assert warned_h["status"] == "degraded" and warned_h["reasons"] == ["No phone is subscribed to push."]
    assert alerted["health"]["status"] == "degraded"
    assert alerted["health"]["reasons"] == ["Atlas loop stalled (4 times)"], "grouped; read and normal ones left out"
    assert {"id": "alerts", "label": "Health alerts unread", "count": 4, "href": "#inbox/notifications"} \
        in alerted["attention"]


async def quiet_ok() -> dict[str, Any]:
    return {"status": "ok", "problems": [], "warnings": []}


async def test_the_attention_list_counts_what_needs_a_look(client, rules, quiet, monkeypatch):
    quiet_sources(monkeypatch, overdue=2, ideas=6, setup_left=4)
    async with livedb.shadow(monkeypatch, *TABLES):
        await _approval("draft_email", {"to": "a@b.test"})
        s = (await client.get("/api/core/state")).json()
    assert [(a["id"], a["count"], a["href"]) for a in s["attention"]] == [
        ("decisions", 1, "#inbox/approvals"), ("overdue", 2, "#plan/week"),
        ("ideas", 6, "#inbox/skills"), ("setup", 4, "#settings/connections")]


async def test_a_broken_attention_source_is_left_out_not_zeroed(rules, monkeypatch):
    from core import planner

    quiet_sources(monkeypatch, ideas=1)

    async def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("planner down")

    monkeypatch.setattr(planner, "agenda", broken)
    async with livedb.shadow(monkeypatch, *TABLES):
        out = await core_view.attention(0)
    assert [a["id"] for a in out] == ["ideas"]


async def test_loop_hand_offs_name_the_purpose_not_the_prompt(rules):
    names = {"ceo": "CEO"}
    for who, purpose in (("loop/brief/morning", "for the morning brief"), ("loop/executive", "for the half-hourly review"),
                         ("night", "for a night-shift task"), ("workflow/weekly_review", "for the weekly review routine")):
        out = await core_view.to_view({"id": 1, "at": datetime.now(UTC), "kind": "dispatch", "agent": who,
                                       "detail": "It is the evening of Wednesday, October 21, 2026. Write the brief.",
                                       "data": {"from": who, "target": "ceo"}}, names)
        assert out[0]["summary"] == f"ATLAS asked CEO {purpose}."


async def test_event_payloads_have_the_contract_shape(rules):
    names = {"coo": "COO", "cto": "CTO", "ceo": "CEO"}
    at = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)
    out = await core_view.to_view({"id": 7, "at": at, "kind": "dispatch", "agent": "ceo",
                                   "detail": "Check the planner tests.", "data": {"from": "ceo", "target": "cto"}},
                                  names)
    msg = out[0]
    assert set(msg) == {"type", "id", "ts", "from", "to", "summary", "kind"}
    assert (msg["type"], msg["from"], msg["to"], msg["kind"]) == ("message", "CEO", "CTO", "")
    assert msg["summary"] == "CEO asked CTO: Check the planner tests."
    assert msg["ts"] == "2026-10-22T13:30:00-05:00", "Houston time, as the briefs use"
    assert out[1] == {"type": "agent_state", "agent": "cto", "state": "receiving",
                      "last_action": msg["summary"]}
    done = await core_view.to_view({"id": 8, "at": at, "kind": "dispatch.done", "agent": "cto",
                                    "data": {"from": "ceo", "target": "cto", "ok": True}}, names)
    assert done[0]["summary"] == "CTO finished the task for CEO." and done[0]["kind"] == "ok"
    run = await core_view.to_view({"id": 9, "at": at, "kind": "run.finished", "agent": "coo",
                                   "title": "Filed two receipts.", "data": {"status": "done"}}, names)
    assert run == [{"type": "agent_state", "agent": "coo", "state": "idle", "last_action": "Filed two receipts."}]
    loops = await core_view.to_view({"id": 10, "at": at, "kind": "dispatch", "agent": "loop/brief/morning",
                                     "detail": "Write the brief.", "data": {"target": "ceo"}}, names)
    assert loops[0]["from"] == "ATLAS" and loops[0]["to"] == "CEO"


async def test_old_dispatch_events_read_the_target_from_the_title(rules):
    out = await core_view.to_view({"id": 1, "at": datetime.now(UTC), "kind": "dispatch", "agent": "ceo",
                                   "title": "asked coo", "detail": "Plan the week.", "data": {}},
                                  {"coo": "COO", "ceo": "CEO"})
    assert (out[0]["from"], out[0]["to"]) == ("CEO", "COO")


# ------------------------------------------------------------------ privacy


async def test_an_excluded_task_is_a_private_item_with_the_names_only(rules):
    names = {"ceo": "CEO", "client_delivery": "Client delivery"}
    for detail in (f"Reply to frontdesk@client-dental.test about {SECRET}", f"Chase the invoice. {BODY}"):
        out = await core_view.to_view({"id": 2, "at": datetime.now(UTC), "kind": "dispatch", "agent": "ceo",
                                       "detail": detail, "data": {"from": "ceo", "target": "client_delivery"}},
                                      names)
        assert out[0]["summary"] == core_view.PRIVATE
        assert (out[0]["from"], out[0]["to"]) == ("CEO", "Client delivery")
        blob = _blob(out)
        for s in ("SECRET", "client-dental", "Jane Roe", "01/02/1970"):
            assert s not in blob


async def test_private_approvals_show_no_subject_body_or_address(client, rules, quiet, monkeypatch):
    rules(reads=True)  # even with model reads on, an excluded recipient stays private
    async with livedb.shadow(monkeypatch, *TABLES):
        await _approval("draft_email", {"to": "Dr <dr@client-dental.test>", "subject": SECRET, "body": BODY})
        await _approval("draft_email", {"to": "pal@friend.test", "subject": "Lunch", "body": "See you at noon."})
        aid = await _approval("draft_email", {"to": "x@y.test", "subject": "Claim", "body": "Member ID: 77"})
        state = (await client.get("/api/core/state")).json()
        events = await core_view.to_view({"id": 3, "at": datetime.now(UTC), "kind": "approval.asked",
                                          "agent": "client_delivery", "title": "client_delivery wants to run draft_email",
                                          "data": {"approval_id": str(aid)}})
    cards = state["decisions"]
    assert [c["title"] for c in cards] == [core_view.PRIVATE, "Approve the email draft to friend.test",
                                           core_view.PRIVATE]
    assert cards[1]["preview"] == "Subject: Lunch"
    blob = _blob(state) + _blob(events)
    for s in ("SECRET", "client-dental", "Jane Roe", "01/02/1970", "Member ID", "SECRETBODY"):
        assert s not in blob
    assert events[0]["summary"] == "Client delivery needs your yes for private item."


async def test_safe_start_hides_subjects_and_recipients(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        await _approval("draft_email", {"to": "Pal <pal@friend.test>", "subject": "Lunch on Friday", "body": "hi"})
        card = (await client.get("/api/core/state")).json()["decisions"][0]
    assert card["title"] == "Approve an email draft"
    assert "Lunch" not in _blob(card) and "friend" not in _blob(card)
    assert card["preview"] == "The subject stays hidden while mail is private (safe-start)."


# ------------------------------------------------------------------ decisions


async def test_approve_is_idempotent_and_decline_after_is_refused(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        aid = await _approval("draft_email", {"to": "a@b.test", "subject": "s", "body": "b"})
        url = f"/api/core/decisions/{aid}/decide"
        first = await client.post(url, json={"decision": "approve", "idempotency_key": "k1"})
        again = await client.post(url, json={"decision": "approve", "idempotency_key": "k1"})
        other_key = await client.post(url, json={"decision": "approve", "idempotency_key": "k2"})
        no = await client.post(url, json={"decision": "decline", "idempotency_key": "k3"})
        row = await db.fetchrow("select status, decided_by from approvals where id = $1", aid)
    assert first.status_code == 200 and first.json() == {"id": str(aid), "status": "approved", "repeat": False}
    assert again.status_code == 200 and again.json()["repeat"] is True
    assert other_key.status_code == 200 and other_key.json()["repeat"] is True
    assert no.status_code == 409 and no.json()["detail"] == "This was already approved."
    assert (row["status"], row["decided_by"]) == ("approved", "owner/today")


async def test_decline(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        aid = await _approval("draft_email", {"to": "a@b.test"})
        r = await client.post(f"/api/core/decisions/{aid}/decide", json={"decision": "decline"})
        status = await db.fetchval("select status from approvals where id = $1", aid)
    assert r.status_code == 200 and r.json()["status"] == "declined" and status == "rejected"


async def test_an_expired_decision_is_refused_in_plain_words(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        aid = await _approval("draft_email", {"to": "a@b.test"}, expires=datetime.now(UTC) - timedelta(minutes=5))
        r = await client.post(f"/api/core/decisions/{aid}/decide", json={"decision": "approve"})
        status = await db.fetchval("select status from approvals where id = $1", aid)
        state = (await client.get("/api/core/state")).json()
    assert r.status_code == 410 and r.json()["detail"].startswith("This expired at")
    assert status == "expired", "an expired card is never approved"
    assert state["decisions"] == []


async def test_unknown_and_bad_decisions(client, rules, quiet, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        missing = await client.post("/api/core/decisions/00000000-0000-0000-0000-000000000000/decide",
                                    json={"decision": "approve"})
        junk = await client.post("/api/core/decisions/not-an-id/decide", json={"decision": "approve"})
        aid = await _approval("draft_email", {"to": "a@b.test"})
        bad = await client.post(f"/api/core/decisions/{aid}/decide", json={"decision": "yes please"})
    assert (missing.status_code, junk.status_code, bad.status_code) == (404, 404, 400)


async def test_the_night_plan_is_a_decision(client, rules, quiet, monkeypatch):
    from core import night

    async with livedb.shadow(monkeypatch, *TABLES):
        for t in ("Refresh the fixtures", "Draft the weekly note"):
            await db.execute("insert into night_tasks (night, title, definition_of_done, agent, budget_usd) "
                             "values ($1, $2, 'done', 'cto', 0.20)", night.night_of(), t)
        cards = (await client.get("/api/core/state")).json()["decisions"]
        card = cards[-1]
        r = await client.post(f"/api/core/decisions/{card['id']}/decide", json={"decision": "approve"})
        statuses = {x["status"] for x in await db.fetch("select status from night_tasks")}
    assert card["kind"] == "night_plan" and card["cost_estimate_usd"] == pytest.approx(0.40)
    assert "Refresh" not in _blob(card), "task titles stay in Missions"
    assert r.status_code == 200 and statuses == {"approved"}


async def test_expiry_emits_a_decision_change(rules, monkeypatch):
    from core import events
    from core.runtime import approval

    seen: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, **kw: Any) -> None:
        seen.append((kind, kw))

    monkeypatch.setattr(events, "emit", emit)
    async with livedb.shadow(monkeypatch, *TABLES):
        aid = await _approval("draft_email", {"to": "a@b.test"}, expires=datetime.now(UTC) - timedelta(minutes=1))
        assert await approval.expire_overdue_approvals() == 1
        out = await core_view.to_view({"id": 4, "at": datetime.now(UTC), "kind": "approval.expired",
                                       "agent": "client_delivery", "data": {"approval_id": str(aid)}})
    assert seen == [("approval.expired", {"agent": "client_delivery", "title": "expired",
                                          "data": {"approval_id": str(aid)}})]
    assert out == [{"type": "decision_changed", "id": str(aid), "status": "expired"}]


# ------------------------------------------------------------------ demo and no fabrication


async def test_demo_never_reads_the_approval_tables(client, monkeypatch):
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("demo touched the database")

    for fn in ("fetch", "fetchrow", "fetchval", "execute"):
        monkeypatch.setattr(core_view.db, fn, boom)
    r = await client.get("/api/core/state?demo=1")
    assert r.status_code == 200 and r.json()["demo"] is True and r.json()["decisions"] == []


async def test_live_with_no_events_sends_no_messages(rules, monkeypatch):
    from contextlib import asynccontextmanager

    from core import events
    from core.web import core_view as routes

    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    # one event the room does not draw: a tool call by something off the ring
    await q.put({"id": 1, "at": datetime.now(UTC), "kind": "run.tool", "agent": "eval", "data": {}})

    @asynccontextmanager
    async def sub():
        yield q

    monkeypatch.setattr(events, "subscription", sub)
    frames: list[str] = []

    async def read() -> None:
        async for f in routes.stream(ping_seconds=0.05):
            frames.append(f)
            if len(frames) >= 3:
                return

    await asyncio.wait_for(read(), timeout=2)
    assert frames[0] == ": connected\n\n"
    assert all(f == ": ping\n\n" for f in frames[1:]), frames
    async with livedb.shadow(monkeypatch, *TABLES):
        assert await core_view.recent() == []


async def test_the_stream_sends_real_hand_offs(rules, monkeypatch):
    from contextlib import asynccontextmanager

    from core import events
    from core.web import core_view as routes

    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await q.put({"id": 5, "at": datetime.now(UTC), "kind": "dispatch", "agent": "ceo",
                 "detail": "Check spend.", "data": {"from": "ceo", "target": "cfo"}})

    @asynccontextmanager
    async def sub():
        yield q

    monkeypatch.setattr(events, "subscription", sub)
    frames: list[str] = []
    async for f in routes.stream(ping_seconds=1):
        frames.append(f)
        if len(frames) >= 3:
            break
    msg = json.loads(frames[1].removeprefix("data: "))
    assert msg["type"] == "message" and (msg["from"], msg["to"]) == ("CEO", "Finance")
    assert json.loads(frames[2].removeprefix("data: "))["type"] == "agent_state"
