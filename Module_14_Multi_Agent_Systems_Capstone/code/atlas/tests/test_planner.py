"""The Planner (core/planner.py): one line in, the week out.

parse_when is pure and tested as a table. Everything else runs against
private copies of deadlines, triggers, projects, signals and night_tasks,
with a fixed "today" (Wednesday 2026-10-14) so weekday arithmetic is exact.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from agents.ceo.tools import build_tools as ceo_build_tools
from core import db, planner
from core.config import settings
from core.projects import upsert_project
from core.signals import Signal, insert_signal
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
TODAY = date(2026, 10, 14)  # a Wednesday
TABLES = ("deadlines", "triggers", "projects", "signals", "night_tasks", "notifications", "episodes")


# ------------------------------------------------------------------ the parser


@pytest.mark.parametrize("text, title, due, at, prio, hint", [
    ("Email the lender by Friday #kirkdale", "Email the lender", date(2026, 10, 16), None, "normal", "kirkdale"),
    ("Ethics essay outline due 10/17 3pm", "Ethics essay outline", date(2026, 10, 17), time(15, 0), "normal", None),
    ("Call the roofer tomorrow 9am !!", "Call the roofer", date(2026, 10, 15), time(9, 0), "high", None),
    ("Look into the Tesla API", "Look into the Tesla API", None, None, "normal", None),
    ("Read chapter 3 before Thursday (due 2026-10-16)", "Read chapter 3 before Thursday", date(2026, 10, 16), None, "normal", None),
    ("Pay the mortgage on the 1st of November", "Pay the mortgage", date(2026, 11, 1), None, "normal", None),
    ("Dentist Oct 3", "Dentist", date(2027, 10, 3), None, "normal", None),          # passed: next year
    ("Submit the form oct 13", "Submit the form", date(2026, 10, 13), None, "normal", None),  # yesterday's grace
    ("Taxes 12/25", "Taxes", date(2026, 12, 25), None, "normal", None),
    ("Renew the plates 1/5/27", "Renew the plates", date(2027, 1, 5), None, "normal", None),
    ("Standup at 9:30", "Standup", None, time(9, 30), "normal", None),
    ("Lunch with Sam noon friday", "Lunch with Sam", date(2026, 10, 16), time(12, 0), "normal", None),
    ("Ship it next friday", "Ship it", date(2026, 10, 23), None, "normal", None),
    ("Gym this wednesday", "Gym", date(2026, 10, 14), None, "normal", None),
    ("Call mom wednesday", "Call mom", date(2026, 10, 21), None, "normal", None),
    ("Plan the sprint next week", "Plan the sprint", date(2026, 10, 19), None, "normal", None),
    ("Invoice Sidework end of month", "Invoice Sidework", date(2026, 10, 31), None, "normal", None),
    ("Budget review next month", "Budget review", date(2026, 11, 1), None, "normal", None),
    ("Clean the garage this weekend", "Clean the garage", date(2026, 10, 17), None, "normal", None),
    ("Follow up in 3 days", "Follow up", date(2026, 10, 17), None, "normal", None),
    ("Check the oil in two weeks", "Check the oil", date(2026, 10, 28), None, "normal", None),
    ("Rebalance in 1 month", "Rebalance", date(2026, 11, 14), None, "normal", None),
    ("Finish the slides today", "Finish the slides", date(2026, 10, 14), None, "normal", None),
    ("Midnight deploy 12am 2026-11-02", "Midnight deploy", date(2026, 11, 2), time(0, 0), "normal", None),
    ("Read the paper someday", "Read the paper", None, None, "low", None),
    ("Buy 2 tickets for the 5k", "Buy 2 tickets for the 5k", None, None, "normal", None),  # no false dates
])
def test_parse_when_reads_dates_times_projects_and_priority(text, title, due, at, prio, hint):
    p = planner.parse_when(text, TODAY)
    assert (p.title, p.due_date, p.due_time, p.priority, p.project_hint) == (title, due, at, prio, hint)


def test_parse_when_keeps_what_it_read():
    p = planner.parse_when("Email the lender by Friday 3pm #kirkdale !!", TODAY)
    assert set(p.spans) == {"#kirkdale", "!!", "by Friday", "3pm"}
    assert planner.parse_when("   ", TODAY).title == ""


# ------------------------------------------------------------------ the tables


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        monkeypatch.setattr(planner, "today_local", lambda: TODAY)
        kirk = await upsert_project("kirkdale", "Example-Group takeover: Kirkdale", kind="client")
        pick = await upsert_project("sidework", "Sidework", kind="own", priority=2)
        hcc = await upsert_project("hcc", "HCC coursework", kind="college")
        yield SimpleNamespace(kirk=kirk, pick=pick, hcc=hcc)


@aio
async def test_quick_add_links_the_project_dates_the_task_and_arms_the_reminder(world):
    t = await planner.quick_add("Email the lender by Friday #kirk", today=TODAY)
    assert t["created"] and t["title"] == "Email the lender" and t["when"] == date(2026, 10, 16)
    assert t["project"]["slug"] == "kirkdale" and t["source"] == "owner" and t["read"]["spans"] == ["#kirk", "by Friday"]
    trig = await db.fetchrow("select fires_at, payload, status from triggers where spec->>'deadline_id' = $1", str(t["id"]))
    assert trig["status"] == "active" and "due today" in trig["payload"]["message"]
    assert trig["fires_at"].astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M") == "2026-10-16 09:00"

    # A project named in the line is linked without a hash; an explicit one wins.
    t2 = await planner.quick_add("Fix the Sidework invoice", today=TODAY)
    assert t2["project"]["slug"] == "sidework" and t2["when"] is None
    assert await db.fetchval("select count(*) from triggers where spec->>'deadline_id' = $1", str(t2["id"])) == 0
    t3 = await planner.quick_add("Fix the Sidework invoice tomorrow 5pm", project="hcc", today=TODAY)
    assert t3["project"]["slug"] == "hcc" and t3["when"] == date(2026, 10, 15)
    assert t3["at"].astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M") == "17:00"
    t4 = await planner.quick_add("Grade the lab", project=world.hcc.id, today=TODAY)
    assert t4["project"]["slug"] == "hcc" and t4["priority"] == "normal"
    with pytest.raises(ValueError):
        await planner.quick_add("#kirk", today=TODAY)


@aio
async def test_a_recording_adds_each_line_once(world):
    ref = str(uuid4())
    a = await planner.quick_add("Read chapter 3 (due 2026-10-16)", source="recording", source_ref=ref, today=TODAY)
    b = await planner.quick_add("read chapter 3 (due 2026-10-16)", source="recording", source_ref=ref, today=TODAY)
    assert a["created"] and not b["created"] and a["id"] == b["id"]
    assert await db.fetchval("select count(*) from deadlines where source_ref = $1", ref) == 1


@aio
async def test_the_agenda_buckets_every_dated_thing(world):
    # Tasks: today, Friday (done), overdue, someday.
    today_task = await planner.quick_add("Call the roofer today 9am", today=TODAY)
    fri = await planner.quick_add("Outline the essay friday #hcc", today=TODAY)
    await planner.complete(fri["id"])
    await db.execute("insert into deadlines (title, due_date, status) values ('Pay the water bill', $1, 'open')", date(2026, 10, 9))
    await planner.quick_add("Look into the Tesla API", today=TODAY)
    # Senses: a Canvas due date, a calendar event updated twice, a cancelled one, an ignored one.
    tz = ZoneInfo(settings.timezone)
    await insert_signal(Signal(source="canvas", external_id="asg-1", occurred_at=datetime.now(UTC),
                               subject="Due: Ethics essay", deadline=date(2026, 10, 15), project_id=world.hcc.id,
                               raw={"assignment_id": 1}))
    for n in (1, 2):
        await insert_signal(Signal(source="gcal", external_id=f"ev-1:v{n}", occurred_at=datetime(2026, 10, 10, n, tzinfo=UTC),
                                   subject="Calendar: Dentist on Thu Oct 15 14:30", deadline=date(2026, 10, 15),
                                   url="https://cal/x", raw={"event_id": "ev-1"}))
    await insert_signal(Signal(source="gcal", external_id="ev-2", occurred_at=datetime.now(UTC),
                               subject="Cancelled: Old standup on Thu Oct 15 09:00", deadline=date(2026, 10, 15), raw={"event_id": "ev-2"}))
    sid = await insert_signal(Signal(source="canvas", external_id="asg-9", occurred_at=datetime.now(UTC),
                                     subject="Due: Dropped quiz", deadline=date(2026, 10, 16), raw={"assignment_id": 9}))
    await db.execute("update signals set triage_status = 'ignored' where id = $1", sid)
    # The night shift and a project's next action.
    await db.execute("insert into night_tasks (night, title, definition_of_done, agent, status, project_id) values ($1, 'Draft the status update', 'a draft', 'coo', 'approved', $2)",
                     date(2026, 10, 14), world.pick.id)
    await db.execute("insert into night_tasks (night, title, definition_of_done, agent, status) values ($1, 'Skipped one', 'x', 'coo', 'skipped')", date(2026, 10, 14))
    await db.execute("update projects set next_action = 'Send the lender letter', next_action_due = $2 where id = $1",
                     world.kirk.id, date(2026, 10, 16))

    plan = await planner.agenda(TODAY, 3, today=TODAY)
    assert [d["label"] for d in plan["days"]] == ["Wed 14", "Thu 15", "Fri 16"] and plan["days"][0]["today"]
    wed, thu, fri_ = (d["items"] for d in plan["days"])
    assert [(i["kind"], i["title"]) for i in wed] == [("task", "Call the roofer"), ("night", "Draft the status update")]
    assert wed[0]["id"] == today_task["id"] and wed[0]["at"].astimezone(tz).strftime("%H:%M") == "09:00"
    assert wed[1]["project"]["slug"] == "sidework" and wed[1]["agent"] == "coo"
    assert [(i["kind"], i["title"]) for i in thu] == [("event", "Dentist"), ("class", "Ethics essay")]
    assert thu[0]["at"].astimezone(tz).strftime("%H:%M") == "14:30" and thu[0]["url"] == "https://cal/x"
    assert thu[1]["project"]["slug"] == "hcc"
    assert [(i["kind"], i["title"], i["done"]) for i in fri_] == [("task", "Outline the essay", True), ("next", "Send the lender letter", False)]
    assert [t["title"] for t in plan["overdue"]] == ["Pay the water bill"]
    assert [t["title"] for t in plan["someday"]] == ["Look into the Tesla API"]
    assert plan["counts"]["today"] == 2 and plan["counts"]["overdue"] == 1 and plan["counts"]["week"] == 6

    text = planner.text_agenda(plan)
    assert text.startswith("Overdue: Pay the water bill (2026-10-09)")
    assert "Today: 09:00 Call the roofer; Draft the status update #sidework [night]" in text
    assert "Thu Oct 15: 14:30 Dentist [cal]; Ethics essay #hcc [class]" in text
    assert "Outline the essay #hcc (done)" in text and "Someday: Look into the Tesla API" in text


@aio
async def test_complete_reopen_drop_reschedule_and_find(world):
    t = await planner.quick_add("Return the library books friday", today=TODAY)
    assert (await planner.complete(t["id"]))["status"] == "done"
    assert (await planner.reopen(t["id"]))["status"] == "open"
    moved = await planner.reschedule(t["id"], date(2026, 10, 20), time(18, 0))
    assert moved["when"] == date(2026, 10, 20) and moved["at"].astimezone(ZoneInfo(settings.timezone)).hour == 18
    assert (await planner.reschedule(t["id"], None))["when"] is None
    await planner.quick_add("Water the plants", today=TODAY)
    assert (await planner.find_task("library books"))["id"] == t["id"]
    assert (await planner.find_task(str(t["id"])[:8]))["id"] == t["id"]
    assert await planner.find_task("the") is None            # two open tasks say "the"
    assert (await planner.drop(t["id"]))["status"] == "cancelled"
    assert await planner.complete(uuid4()) is None and await planner.reschedule(uuid4(), None) is None


# ------------------------------------------------------------------ chat and routes


@aio
async def test_the_ceo_can_add_read_and_finish_tasks(world):
    tools = {t.name: t.handler for t in ceo_build_tools()}
    added = await tools["add_task"]({"text": "Email the lender by Friday #kirk"})
    assert added["title"] == "Email the lender" and added["due_date"] == "2026-10-16" and added["project"] == "kirkdale"
    week = await tools["plan_week"]({"days": 7, "start": "2026-10-14"})
    assert "Fri Oct 16: Email the lender #kirkdale" in week["agenda"] and week["counts"]["week"] >= 1
    done = await tools["complete_task"]({"task": "Email the lender"})
    assert done["status"] == "done" and done["task_id"] == added["task_id"]
    assert "error" in await tools["complete_task"]({"task": "nothing like this"})
    with pytest.raises(ValueError):
        await tools["add_task"]({"text": "   "})


@aio
async def test_routes(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/plan/parse", params={"text": "Email the lender by Friday 3pm #kirkdale"})
        assert r.status_code == 200
        assert r.json()["title"] == "Email the lender" and r.json()["project"] == "kirkdale" and r.json()["time"] == "15:00"
        r = await c.post("/api/plan/tasks", json={"text": "Email the lender by Friday #kirk"})
        assert r.status_code == 200 and r.json()["project"]["slug"] == "kirkdale"
        tid = r.json()["id"]
        assert (await c.post("/api/plan/tasks", json={"text": "#kirk"})).status_code == 422
        r = await c.get("/api/plan", params={"start": "2026-10-14", "days": 3})
        assert r.status_code == 200 and [d["label"] for d in r.json()["days"]] == ["Wed 14", "Thu 15", "Fri 16"]
        assert any(i["id"] == tid for i in r.json()["days"][2]["items"])
        assert (await c.get("/api/plan", params={"start": "friday"})).status_code == 422
        r = await c.post(f"/api/plan/tasks/{tid}/reschedule", json={"due_date": "2026-10-20", "time": "18:00"})
        assert r.status_code == 200 and r.json()["when"] == "2026-10-20"
        assert (await c.post(f"/api/plan/tasks/{tid}/reschedule", json={"due_date": "2026-10-20", "time": "6pm"})).status_code == 422
        assert (await c.post(f"/api/plan/tasks/{tid}/done")).json()["status"] == "done"
        assert (await c.post(f"/api/plan/tasks/{tid}/reopen")).json()["status"] == "open"
        assert (await c.post(f"/api/plan/tasks/{tid}/drop")).json()["status"] == "cancelled"
        assert (await c.post(f"/api/plan/tasks/{uuid4()}/done")).status_code == 404
        assert (await c.post(f"/api/plan/tasks/{tid}/explode")).status_code == 422
