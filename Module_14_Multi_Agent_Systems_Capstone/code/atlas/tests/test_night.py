"""The night shift (core/night.py): proposals from the project list,
Trilok's approve/skip, the late run inside the nightly cap, the morning
report, his Accept/Rework, and the routes.

The agent is a fake that answers DONE:, NEEDS YOU:, prose, or raises. Live
test database with every table the night shift touches as a private copy
(tests/livedb.py), so nothing here can reach Trilok's real projects or
notifications.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, night, projects, standard
from core.costs import CostCapExceeded
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")

TABLES = ("night_tasks", "projects", "episodes", "notifications", "system_heartbeats", "actions", "approvals",
          "tool_ledger", "job_events", "job_applications", "approval_policy", "connector_state", "llm_calls", "events")

EVENING = datetime(2026, 10, 11, 22, 30, tzinfo=UTC)   # 17:30 in Houston: tonight is Oct 11
MORNING = datetime(2026, 10, 12, 12, 30, tzinfo=UTC)   # 07:30 in Houston: still night Oct 11


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private_tables(monkeypatch):
    monkeypatch.setattr(night.settings, "timezone", "America/Chicago")
    async with livedb.shadow(monkeypatch, *TABLES):
        yield


class FakeDispatch:
    """Answers per agent: a reply string, or an exception to raise."""

    def __init__(self, replies: dict[str, object]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, agent, task, *, dispatched_by, participants):
        self.calls.append((agent, task))
        reply = self.replies.get(agent, "DONE: saved it.")
        if isinstance(reply, Exception):
            raise reply
        ep = await db.fetchval(
            "insert into episodes (started_at, ended_at, agent_name, summary) values (now(), now(), $1, $2) returning id",
            agent, f"{agent}: {task[:60]}",
        )
        return SimpleNamespace(text=reply, episode_id=ep, turns=2, stop_reason="end_turn",
                               usage={"input_tokens": 2000, "output_tokens": 200})


async def _three_projects() -> None:
    await projects.upsert_project("test-hvac", "Demo HVAC", kind="client", priority=1)
    await projects.set_next_action((await projects.get_project("test-hvac")).id, "Draft the October status update",
                                   date(2026, 10, 13))
    await projects.upsert_project("test-hcc", "HCC", kind="college", priority=2)
    await projects.set_next_action((await projects.get_project("test-hcc")).id, "Outline the ethics essay")
    await projects.upsert_project("test-idle", "Idle", kind="own", priority=3)  # no next action: nothing to propose


# ------------------------------------------------------------------ planning


def test_the_night_belongs_to_the_evening_until_noon(monkeypatch):
    monkeypatch.setattr(night.settings, "timezone", "America/Chicago")
    assert night.night_of(EVENING) == date(2026, 10, 11) and night.phase(EVENING) == "evening"
    assert night.night_of(MORNING) == date(2026, 10, 11) and night.phase(MORNING) == "morning"
    assert night.night_of(datetime(2026, 10, 12, 18, 0, tzinfo=UTC)) == date(2026, 10, 12)  # 13:00: a new day


@aio
async def test_proposals_come_from_project_next_actions_once():
    await _three_projects()
    added = await night.propose(EVENING)
    assert [(t["title"], t["agent"]) for t in added] == [
        ("Draft the October status update", "client_delivery"), ("Outline the ethics essay", "academic")]
    assert "due 2026-10-13" in added[0]["definition_of_done"] and added[0]["status"] == "proposed"
    assert await night.propose(EVENING) == []  # already on tonight's list
    n = await db.fetchrow("select title, data from notifications where kind = 'night_proposed'")
    assert n["title"] == "Tonight: 2 tasks proposed" and n["data"] == {"view": "home"}


@aio
async def test_owner_tasks_start_approved_and_are_checked():
    await _three_projects()
    t = await night.add_task("Research Databricks interview loop", "A one-page prep note saved with save_materials.",
                             project="test-hvac", now=EVENING)
    assert t["status"] == "approved" and t["agent"] == "client_delivery" and t["night"] == date(2026, 10, 11)
    t2 = await night.add_task("Tidy the CTO radar", "A radar note exists.", agent="CTO", now=EVENING)
    assert t2["agent"] == "cto" and t2["project_id"] is None
    with pytest.raises(ValueError, match="no agent"):
        await night.add_task("x", "y", agent="nobody")
    with pytest.raises(ValueError, match="no project"):
        await night.add_task("x", "y", project="nope")
    with pytest.raises(ValueError, match="definition of done"):
        await night.add_task("x", "  ")


@aio
async def test_decide_and_approve_all():
    await _three_projects()
    a, b = await night.propose(EVENING)
    assert (await night.decide(a["id"], "approve"))["status"] == "approved"
    assert await night.decide(a["id"], "approve") is None            # not proposed any more
    assert (await night.decide(a["id"], "skip"))["status"] == "skipped"  # an approved task can still be skipped
    assert await night.approve_all(EVENING) == 1                       # b
    assert (await night.listing(EVENING))["tasks"][1]["status"] == "approved"
    with pytest.raises(ValueError):
        await night.decide(b["id"], "maybe")


# ------------------------------------------------------------------ the run and the morning


@aio
async def test_run_night_works_each_task_and_the_morning_reports_it():
    await _three_projects()
    await night.propose(EVENING)
    await night.add_task("Reconcile September", "The P&L draft is saved.", agent="cfo", now=EVENING)
    await night.add_task("Watch the shared reel", "A radar note exists.", agent="cto", now=EVENING)
    await night.approve_all(EVENING)
    fake = FakeDispatch({
        "client_delivery": "DONE: the status update is in Gmail drafts.\nThree bullets, one ask.",
        "academic": "NEEDS YOU: which of the two essay prompts did you pick?",
        "cfo": "I looked at the statements and they seem fine.",   # never said DONE
        "cto": RuntimeError("ollama offline"),
    })
    out = await night.run_night(dispatch_fn=fake, now=EVENING)
    assert out == {**out, "done": 1, "needs_you": 2, "failed": 1, "held": 0}
    assert Decimal(out["spent"]) > 0
    # Best project first, then the owner's additions in the order added.
    assert [c[0] for c in fake.calls] == ["client_delivery", "academic", "cfo", "cto"]
    assert "Definition of done: Draft the October status update is finished" in fake.calls[0][1]
    assert "Never send, submit, post or pay" in fake.calls[0][1]

    tasks = {t["title"]: t for t in (await night.listing(MORNING))["tasks"]}
    assert tasks["Draft the October status update"]["status"] == "done"
    assert tasks["Draft the October status update"]["episode_id"] and Decimal(tasks["Draft the October status update"]["cost_usd"]) > 0
    assert tasks["Outline the ethics essay"]["status"] == "needs_you"
    assert tasks["Reconcile September"]["result"].startswith("NEEDS YOU: the agent did not report DONE")
    assert tasks["Watch the shared reel"]["status"] == "failed" and "ollama offline" in tasks["Watch the shared reel"]["error"]
    assert (await night.listing(MORNING))["phase"] == "morning"

    report = await night.morning_report(MORNING)
    assert report is not None and report.startswith("Night shift: 1 done, 2 need you, 1 failed, $")
    assert "+ Draft the October status update (client_delivery): DONE: the status update is in Gmail drafts. #run/" in report
    assert "! Outline the ethics essay (academic): NEEDS YOU: which of the two essay prompts" in report
    assert "x Watch the shared reel (cto): RuntimeError: ollama offline" in report
    n = await db.fetchrow("select priority, data from notifications where kind = 'night_report'")
    assert n["priority"] == "high" and n["data"] == {"view": "home"}
    assert await night.morning_report(MORNING) is None  # reported once


@aio
async def test_the_nightly_cap_holds_the_rest_and_the_daily_cap_stops_the_night():
    await _three_projects()
    await night.propose(EVENING)
    await night.add_task("Third", "x", agent="cfo", now=EVENING)
    await night.approve_all(EVENING)
    fake = FakeDispatch({})
    out = await night.run_night(dispatch_fn=fake, now=EVENING, cap_usd=0.001)
    assert out["done"] == 1 and out["held"] == 2 and len(fake.calls) == 1
    held = [t for t in (await night.listing(EVENING))["tasks"] if t["status"] == "held"]
    assert all("nightly cap was reached" in t["error"] for t in held)

    await db.execute("update night_tasks set status = 'approved', error = null where status = 'held'")
    fake = FakeDispatch({"academic": CostCapExceeded(Decimal("8.01"), Decimal("8"))})
    out = await night.run_night(dispatch_fn=fake, now=EVENING, cap_usd=50)
    assert out["held"] == 2 and len(fake.calls) == 1  # the first hit the daily cap; nothing else was tried
    assert all("daily cap" in t["error"] for t in (await night.listing(EVENING))["tasks"] if t["status"] == "held")


@aio
async def test_review_feeds_the_overnight_yield_measure():
    await _three_projects()
    await night.propose(EVENING)
    await night.approve_all(EVENING)
    await night.run_night(dispatch_fn=FakeDispatch({}), now=EVENING)
    a, b = (await night.listing(MORNING))["tasks"]
    rows = {r["key"]: r for r in (await standard.scorecard())["rows"]}
    assert rows["overnight_yield"]["status"] == "none" and "No night-shift result judged yet" in rows["overnight_yield"]["detail"]

    assert (await night.review(a["id"], "accepted"))["review"] == "accepted"
    assert (await night.review(b["id"], "rework", note="too long"))["review_note"] == "too long"
    assert await night.review(uuid4(), "accepted") is None
    rows = {r["key"]: r for r in (await standard.scorecard())["rows"]}
    assert rows["overnight_yield"]["value"] == "1 of 2 accepted" and rows["overnight_yield"]["status"] == "fail"
    assert await night.yield_so_far() == {"reviewed": 2, "accepted": 1}


# ------------------------------------------------------------------ the night as a film


@aio
async def test_replay_cuts_the_film_from_the_nights_window():
    from core import events

    night_date = date(2026, 10, 11)
    start, end = night.window(night_date)
    assert (start.hour, end.hour, (end - start).total_seconds()) == (22, 9, 11 * 3600)
    ep = uuid4()

    async def at(minutes: int, kind: str, **cols):
        payload = {"agent": None, "episode_id": None, "title": "", "detail": "", "data": {}, **cols}
        await db.execute(
            "insert into events (at, kind, agent, episode_id, title, detail, data) values ($1, $2, $3, $4, $5, $6, $7)",
            start + timedelta(minutes=minutes), kind, payload["agent"], payload["episode_id"], payload["title"],
            payload["detail"], payload["data"],
        )

    await at(-30, "run.started", agent="ceo", title="before the window")          # 21:30: not in the film
    await at(95, "night.task", agent="career", title="Draft the status update", data={"status": "running"})
    await at(96, "run.started", agent="career", episode_id=ep, title="starting")
    await at(97, "run.tool", agent="career", episode_id=ep, title="read_resume")
    await at(98, "run.tool.done", agent="career", episode_id=ep, title="read_resume", data={"ok": True})
    await at(99, "submit.shot", agent="career", title="FDE at Databricks", detail="filled", data={"url": "/x.png"})
    await at(100, "approval.asked", agent="career", title="Send it?")
    await at(101, "run.finished", agent="career", episode_id=ep, title="done", data={"cost": "0.12", "status": "ok"})
    await at(102, "notified", title="Night shift: 1 done")
    await at(11 * 60 + 1, "run.started", agent="coo", title="after the window")  # 09:01: not in the film
    await _three_projects()
    await night.propose(EVENING)

    film = await night.replay(night_date)
    assert film["night"] == "2026-10-11" and film["window"]["start"].startswith("2026-10-11T22:00")
    assert [e["kind"] for e in film["timeline"]] == ["night.task", "run.started", "run.tool", "run.tool.done",
                                                      "submit.shot", "approval.asked", "run.finished", "notified"]
    assert film["totals"] == {"events": 8, "runs": 1, "tool_calls": 1, "approvals_asked": 1, "screenshots": 1,
                              "pushes": 1, "cost": "0.12", "agents": ["career"], "done": 0, "needs_you": 0}
    assert [t["title"] for t in film["tasks"]] == ["Draft the October status update", "Outline the ethics essay"]
    assert events.recent  # the film is cut from the same stream the dashboard watches


# ------------------------------------------------------------------ routes


@aio
async def test_routes(monkeypatch):
    await _three_projects()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/night")
        assert r.status_code == 200 and r.json()["tasks"] == [] and "cfo" in r.json()["agents"]
        assert any(p["slug"] == "test-hvac" for p in r.json()["projects"])
        r = await c.post("/api/night/tasks", json={"title": "Prep", "definition_of_done": "A note.", "project": "test-hcc"})
        assert r.status_code == 200 and r.json()["task"]["agent"] == "academic"
        assert (await c.post("/api/night/tasks", json={"title": "x", "definition_of_done": "y", "agent": "nobody"})).status_code == 422
        tid = r.json()["task"]["id"]
        assert (await c.post(f"/api/night/tasks/{tid}/decide", json={"decision": "approve"})).status_code == 409
        assert (await c.post(f"/api/night/tasks/{tid}/decide", json={"decision": "skip"})).json()["task"]["status"] == "skipped"
        assert (await c.post(f"/api/night/tasks/{tid}/review", json={"verdict": "accepted"})).status_code == 409

        r = await c.get("/api/night/replay?night=2026-10-11")
        assert r.status_code == 200 and r.json()["night"] == "2026-10-11" and r.json()["timeline"] == []
        assert (await c.get("/api/night/replay?night=yesterday")).status_code == 422
        assert (await c.get("/api/night/replay")).status_code == 200

        monkeypatch.setattr("core.runtime.dispatcher.dispatch", FakeDispatch({}))
        r = await c.post("/api/night/propose")
        assert r.status_code == 200 and r.json()["proposed"] >= 1
        assert (await c.post("/api/night/approve-all")).json()["approved"] >= 1
        r = await c.post("/api/night/run")
        assert r.status_code == 200 and r.json()["done"] >= 1
        done = [t for t in (await c.get("/api/night")).json()["tasks"] if t["status"] == "done"][0]
        assert (await c.post(f"/api/night/tasks/{done['id']}/review", json={"verdict": "accepted", "note": "good"})).json()["task"]["review"] == "accepted"
