"""Recurring tasks (core/recurring.py): a rule in his words, dated work on the Planner."""

from __future__ import annotations

from datetime import date, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from core import db, planner, recurring
from core.config import settings
from core.projects import upsert_project
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
TODAY = date(2026, 10, 14)  # a Wednesday


# ------------------------------------------------------------------ the phrase


@pytest.mark.parametrize("text, rule, rest", [
    ("Pay the mortgage every month on the 1st", {"cadence": "monthly", "day": 1}, "Pay the mortgage"),
    ("Statement reconciliation monthly on the 15th", {"cadence": "monthly", "day": 15}, "Statement reconciliation"),
    ("Rent every 1st", {"cadence": "monthly", "day": 1}, "Rent"),
    ("Backups every last day of the month", {"cadence": "monthly", "day": -1}, "Backups"),
    ("Estimated taxes every quarter on the 15th", {"cadence": "quarterly", "day": 15, "months": [1, 4, 7, 10]}, "Estimated taxes"),
    ("Investor update quarterly", {"cadence": "quarterly", "day": 1, "months": [1, 4, 7, 10]}, "Investor update"),
    ("Renew the plates every year on April 15", {"cadence": "annual", "month": 4, "day": 15}, "Renew the plates"),
    ("Insurance renewal annually on Aug 30", {"cadence": "annual", "month": 8, "day": 30}, "Insurance renewal"),
    ("Birthday card every Oct 3", {"cadence": "annual", "month": 10, "day": 3}, "Birthday card"),
    ("Team review every friday", {"cadence": "weekly", "weekday": 4}, "Team review"),
    ("Standup weekly on monday", {"cadence": "weekly", "weekday": 0}, "Standup"),
    ("Groceries every week", {"cadence": "weekly", "weekday": 2}, "Groceries"),          # today's weekday
    ("Call mom on Sunday", None, "Call mom on Sunday"),                                  # a date, not a rule
    ("Review the 3rd quarter numbers", None, "Review the 3rd quarter numbers"),
])
def test_parse_rule(text, rule, rest):
    got, _phrase, remaining = recurring.parse_rule(text, TODAY)
    assert got == rule and remaining == rest


def test_describe_and_occurrences():
    assert recurring.describe({"cadence": "monthly", "day": 1}) == "every month on the 1st"
    assert recurring.describe({"cadence": "monthly", "day": 22}) == "every month on the 22nd"
    assert recurring.describe({"cadence": "monthly", "day": -1}) == "every month on the last day"
    assert recurring.describe({"cadence": "quarterly", "day": 15}) == "every quarter on the 15th"
    assert recurring.describe({"cadence": "annual", "month": 4, "day": 15}) == "every year on April 15"
    assert recurring.describe({"cadence": "weekly", "weekday": 4}) == "every Friday"

    assert recurring.occurrences({"cadence": "weekly", "weekday": 4}, date(2026, 10, 14), date(2026, 11, 4)) == [
        date(2026, 10, 16), date(2026, 10, 23), date(2026, 10, 30)]
    assert recurring.occurrences({"cadence": "monthly", "day": 31}, date(2026, 10, 14), date(2027, 1, 5)) == [
        date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31)]                      # clamped to the month
    assert recurring.occurrences({"cadence": "monthly", "day": -1}, date(2026, 2, 1), date(2026, 3, 31)) == [
        date(2026, 2, 28), date(2026, 3, 31)]
    assert recurring.occurrences({"cadence": "quarterly", "day": 15, "months": [1, 4, 7, 10]},
                                 date(2026, 10, 14), date(2027, 4, 30)) == [date(2026, 10, 15), date(2027, 1, 15), date(2027, 4, 15)]
    assert recurring.occurrences({"cadence": "annual", "month": 4, "day": 15}, date(2026, 10, 14), date(2027, 12, 31)) == [date(2027, 4, 15)]
    assert recurring.occurrences({"cadence": "annual", "month": 4, "day": 15}, date(2026, 10, 14), date(2026, 10, 1)) == []
    assert recurring.next_due({"cadence": "monthly", "day": 1}, date(2026, 10, 14)) == date(2026, 11, 1)
    assert recurring.next_due({"cadence": "monthly", "day": 14}, date(2026, 10, 14)) == date(2026, 11, 14)  # strictly after


# ------------------------------------------------------------------ the rows


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, "task_templates", "deadlines", "triggers", "projects", "signals", "night_tasks"):
        monkeypatch.setattr(planner, "today_local", lambda: TODAY)
        monkeypatch.setattr(recurring, "_today", lambda: TODAY)
        home = await upsert_project("home", "The house", kind="personal")
        yield home


@aio
async def test_a_rule_writes_its_tasks_once_and_the_horizon_fills_daily(world):
    t = await planner.quick_add("Pay the mortgage every month on the 1st #home 9am", today=TODAY)
    assert t["created"] and t["rule"]["describe"] == "every month on the 1st" and t["rule"]["time"] == "09:00"
    assert t["kind"] == "task" and t["when"] == date(2026, 11, 1) and t["repeats"] and t["project"]["slug"] == "home"
    assert t["read"]["rule"] == "every month on the 1st" and t["read"]["spans"] == ["#home", "every month on the 1st", "9am"]
    rows = await db.fetch("select title, due_date, due_at, source, source_ref from deadlines order by due_date")
    assert [(r["title"], r["due_date"]) for r in rows] == [("Pay the mortgage", date(2026, 11, 1))]
    assert rows[0]["source"] == "recurring" and rows[0]["source_ref"] == str(t["rule"]["id"])
    assert rows[0]["due_at"].astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M") == "09:00"
    assert await db.fetchval("select count(*) from triggers where spec->>'deadline_id' = $1", str(t["id"])) == 1

    # The daily job: nothing new inside the same horizon; a later day brings the next one, once.
    assert await recurring.materialize(today=TODAY) == []
    later = await recurring.materialize(today=date(2026, 11, 12))
    assert [x["when"] for x in later] == [date(2026, 12, 1)] and await recurring.materialize(today=date(2026, 11, 12)) == []
    assert await db.fetchval("select count(*) from deadlines") == 2

    plan = await planner.agenda(date(2026, 10, 29), 7, today=TODAY)
    assert [r["describe"] for r in plan["rules"]] == ["every month on the 1st"] and plan["rules"][0]["next"] == date(2026, 11, 1)
    nov1 = next(d for d in plan["days"] if d["date"] == date(2026, 11, 1))
    assert [(i["title"], i["repeats"]) for i in nov1["items"]] == [("Pay the mortgage", True)]

    # Stopping the rule drops what is not due yet and keeps the past.
    await db.execute("update deadlines set status = 'done' where due_date = $1", date(2026, 11, 1))
    stopped = await recurring.stop_rule(t["rule"]["id"])
    assert stopped["status"] == "paused"
    statuses = {r["due_date"]: r["status"] for r in await db.fetch("select due_date, status from deadlines")}
    assert statuses == {date(2026, 11, 1): "done", date(2026, 12, 1): "cancelled"}
    assert await recurring.stop_rule(t["rule"]["id"]) is None and await recurring.list_rules(TODAY) == []
    assert await recurring.materialize(today=date(2027, 1, 1)) == []


@aio
async def test_a_rule_whose_first_date_is_far_away_still_stands(world):
    t = await planner.quick_add("Renew the plates every year on April 15", today=TODAY)
    assert t["kind"] == "rule" and t["when"] == date(2027, 4, 15) and t["created"]
    assert await db.fetchval("select count(*) from deadlines") == 0
    assert (await recurring.list_rules(TODAY))[0]["describe"] == "every year on April 15"
    written = await recurring.materialize(today=date(2027, 4, 1))
    assert [w["when"] for w in written] == [date(2027, 4, 15)]


@aio
async def test_quarterly_taxes_land_four_times_a_year(world):
    t = await planner.quick_add("Estimated taxes every quarter on the 15th", today=TODAY)
    assert t["when"] == date(2026, 10, 15) and t["rule"]["describe"] == "every quarter on the 15th"
    written = await recurring.materialize(today=date(2027, 1, 1), horizon_days=200)
    assert [w["when"] for w in written] == [date(2027, 1, 15), date(2027, 4, 15), date(2027, 7, 15)]


@aio
async def test_routes(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/plan/parse", params={"text": "Team review every friday 3pm #home"})
        assert r.status_code == 200 and r.json()["rule"] == "every Friday" and r.json()["time"] == "15:00"
        assert r.json()["title"] == "Team review" and r.json()["date"] == "2026-10-16"
        r = await c.post("/api/plan/tasks", json={"text": "Team review every friday 3pm #home"})
        assert r.status_code == 200 and r.json()["repeats"] and r.json()["rule"]["describe"] == "every Friday"
        rid = r.json()["rule"]["id"]
        r = await c.get("/api/plan", params={"start": "2026-10-14", "days": 7})
        assert [x["describe"] for x in r.json()["rules"]] == ["every Friday"]
        assert any(i["title"] == "Team review" and i["repeats"] for i in r.json()["days"][2]["items"])
        assert (await c.post(f"/api/plan/rules/{rid}/stop")).json()["status"] == "paused"
        assert (await c.post(f"/api/plan/rules/{rid}/stop")).status_code == 404
        assert (await c.post(f"/api/plan/rules/{uuid4()}/stop")).status_code == 404


@aio
async def test_add_rule_rejects_an_unknown_cadence(world):
    with pytest.raises(ValueError):
        await recurring.add_rule("x", {"cadence": "hourly"})
    assert time.fromisoformat("09:00") == time(9, 0)   # the time a rule carries is stored as HH:MM
