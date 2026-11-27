"""The Bridge's dial in one call: /api/day composes the last 24 hours of
finished work and the next 24 hours of the plan. Its sources are stubbed,
so these tests never touch the database."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from core import day as day_mod
from core.config import settings
from core.web.api import app

TZ = ZoneInfo(settings.timezone)
NOW = datetime(2026, 10, 15, 7, 30, tzinfo=TZ)


def _ev(at: datetime | None, kind: str = "run.finished", **kw: Any) -> dict[str, Any]:
    return {"id": kw.pop("id", 1), "at": at.isoformat() if at else None, "kind": kind,
            "agent": kw.pop("agent", "coo"), "episode_id": kw.pop("episode_id", None),
            "title": kw.pop("title", "did a thing"), "detail": "", "data": kw.pop("data", {})}


def _plan(items_by_day: dict[date, list[dict[str, Any]]]) -> dict[str, Any]:
    return {"days": [{"date": d, "items": its} for d, its in items_by_day.items()]}


@pytest.fixture
def sources(monkeypatch):
    calls: dict[str, Any] = {"events": [], "agenda": {}}

    async def recent(**kw: Any) -> list[dict[str, Any]]:
        calls["events_kw"] = kw
        return list(calls["events"])

    async def agenda(start: date | None = None, days: int = 7, **kw: Any) -> dict[str, Any]:
        calls["agenda_args"] = (start, days)
        return _plan(calls["agenda"])

    monkeypatch.setattr(day_mod.events, "recent", recent)
    monkeypatch.setattr(day_mod.planner, "agenda", agenda)
    return calls


async def test_day_window_is_last_24_hours(sources):
    inside = NOW - timedelta(hours=23, minutes=59)
    outside = NOW - timedelta(hours=24, seconds=1)
    sources["events"] = [_ev(outside, id=1), _ev(inside, id=2, title="kept")]
    out = await day_mod.day(NOW)
    assert sources["events_kw"]["start"] == NOW - timedelta(hours=24)
    assert sources["events_kw"]["end"] == NOW
    assert [d["title"] for d in out["done"]] == ["kept"]


async def test_day_keeps_only_finished_night_tasks_and_recordings(sources):
    t = NOW - timedelta(hours=6)
    sources["events"] = [
        _ev(t, "night.task", title="running", data={"status": "running"}),
        _ev(t, "night.task", title="finished", data={"status": "done"}),
        _ev(t, "night.task", title="needs him", data={"status": "needs_you"}),
        _ev(t, "recording", title="transcribing", data={"status": "uploaded"}),
        _ev(t, "recording", title="notes", data={"status": "done"}),
    ]
    out = await day_mod.day(NOW)
    assert [d["title"] for d in out["done"]] == ["finished", "needs him", "notes"]


async def test_day_counts_a_night_task_once(sources):
    # The night shift emits night.task (done) and run.finished for the same
    # run; the dial shows the task, once, and still counts the run's cost.
    t = NOW - timedelta(hours=7)
    sources["events"] = [
        _ev(t, "run.finished", title="coo: triage the inbox", episode_id="e1", data={"cost": "0.31"}),
        _ev(t + timedelta(seconds=2), "night.task", title="Triage the inbox", episode_id="e1", data={"status": "done"}),
        _ev(t + timedelta(hours=1), "run.finished", title="ceo: answer him", episode_id="e2", data={"cost": "0.04"}),
    ]
    out = await day_mod.day(NOW)
    assert [d["title"] for d in out["done"]] == ["Triage the inbox", "ceo: answer him"]
    assert out["totals"]["done"] == 2 and out["totals"]["runs"] == 2 and out["totals"]["cost"] == "0.35"


async def test_day_skips_events_without_time(sources):
    sources["events"] = [_ev(None, title="no time"), _ev(NOW - timedelta(hours=1), title="ok")]
    out = await day_mod.day(NOW)
    assert [d["title"] for d in out["done"]] == ["ok"]


async def test_day_next_is_the_coming_24_hours(sources):
    today, tomorrow = NOW.date(), NOW.date() + timedelta(days=1)
    sources["agenda"] = {
        today: [
            {"kind": "task", "title": "lender", "at": NOW + timedelta(hours=2)},
            {"kind": "task", "title": "all day today", "at": None},
        ],
        tomorrow: [
            {"kind": "calendar", "title": "too far", "at": NOW + timedelta(hours=25)},
            {"kind": "task", "title": "all day tomorrow", "at": None},
        ],
    }
    out = await day_mod.day(NOW)
    titles = [n["title"] for n in out["next"] if n["kind"] != "night"]
    assert "lender" in titles and "too far" not in titles
    allday = {n["title"]: n for n in out["next"] if n.get("all_day")}
    # 09:00 today is still ahead at 07:30; 09:00 tomorrow is past the 24-hour window.
    assert "all day today" in allday and "all day tomorrow" not in allday
    assert datetime.fromisoformat(allday["all day today"]["at"]).hour == 9
    assert sources["agenda_args"] == (today, 2)


async def test_day_adds_the_night_slots(sources):
    out = await day_mod.day(NOW)
    night = {n["title"]: datetime.fromisoformat(n["at"]) for n in out["next"] if n["kind"] == "night"}
    assert night["Night shift proposes tonight's work"].strftime("%H:%M") == "22:30"
    assert night["Night shift runs"].strftime("%H:%M") == "23:30"
    ats = [n["at"] for n in out["next"]]
    assert ats == sorted(ats)


async def test_day_totals(sources):
    t = NOW - timedelta(hours=8)
    sources["events"] = [
        _ev(t, data={"cost": "0.5"}, episode_id="a"),
        _ev(t + timedelta(hours=1), data={"cost": "1.0"}, episode_id="b"),
        _ev(t + timedelta(hours=3), data={"cost": "0.34"}, episode_id="c"),
    ]
    out = await day_mod.day(NOW)
    assert out["totals"]["cost"] == "1.84"
    assert out["totals"]["runs"] == 3
    assert out["totals"]["done"] == 3
    assert out["totals"]["hours"] == 3.0


async def test_day_route(sources):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/day")
    assert r.status_code == 200
    assert {"now", "tz", "night", "done", "next", "totals"} <= set(r.json())
