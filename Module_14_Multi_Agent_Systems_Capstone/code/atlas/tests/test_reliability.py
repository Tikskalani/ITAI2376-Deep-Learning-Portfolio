"""Reliability (r12): the late briefs of 2026-10-19 and 10-20 and what stops them.

The laptop slept through 20:00, the liveness switch killed the worker on
wake (the sleep looked like a blocked loop), the new worker crashed while
Postgres was still coming back, and the evening brief ran at 22:09 saying
only "Atlas was not running". Every clock here is fake; no model is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from core import liveness, reliability
from core.config import settings
from core.loop import briefs
from core.loop.health import stale_after_min
from core.loop.scheduler import build_scheduler
from tests import livedb

TZ = ZoneInfo(settings.timezone)


def _local(h: int, m: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


@pytest.fixture
def slots(monkeypatch):
    monkeypatch.setattr(settings, "brief_morning", time(8, 0))
    monkeypatch.setattr(settings, "brief_evening", time(20, 0))
    briefs._ATTEMPTED.clear()
    yield
    briefs._ATTEMPTED.clear()


# ------------------------------------------------------------------ liveness and sleep


def test_a_sleeping_laptop_is_not_a_blocked_event_loop():
    # Three hours asleep: the wall clock jumps, the awake clock does not.
    with patch.dict(liveness._state, {"pulse": 1000.0, "cycle": 1000.0, "wall": 50_000.0, "awake": 1000.0}):
        assert liveness.check(1060.0) is None
        gap = liveness.slept(wall=50_000.0 + 3 * 3600 + 60, awake=1060.0)
        assert gap is not None
        assert gap[1] - gap[0] == pytest.approx(3 * 3600)
        # The next pulse after the wake sees no new sleep.
        assert liveness.slept(wall=50_000.0 + 3 * 3600 + 90, awake=1090.0) is None


def test_a_really_blocked_loop_still_dies():
    with patch.dict(liveness._state, {"pulse": 1000.0, "cycle": 1000.0}):
        assert "event loop blocked" in str(liveness.check(1000.0 + liveness.PULSE_LIMIT_S + 1))


def test_a_short_gap_is_not_a_sleep():
    with patch.dict(liveness._state, {"wall": 100.0, "awake": 100.0}):
        assert liveness.slept(wall=190.0, awake=130.0) is None


async def test_the_pulse_hands_a_sleep_to_on_resume(monkeypatch):
    seen: list[tuple[datetime, datetime]] = []

    async def on_resume(frm, to):
        seen.append((frm, to))
        raise RuntimeError("a failed hook must not stop the pulse")

    calls = {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        raise StopAsyncIteration

    monkeypatch.setattr(liveness, "slept", lambda: (1_000.0, 4_600.0))
    monkeypatch.setattr(liveness.asyncio, "sleep", fake_sleep)
    with pytest.raises(StopAsyncIteration):
        await liveness._pulse_forever(on_resume)
    assert seen == [(datetime.fromtimestamp(1_000.0, UTC), datetime.fromtimestamp(4_600.0, UTC))]


# ------------------------------------------------------------------ boot


async def test_the_worker_waits_for_postgres_instead_of_crashing():
    attempts = {"n": 0}
    waits: list[float] = []

    async def connect():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ConnectionRefusedError("[WinError 1225] refused")

    async def sleep(s):
        waits.append(s)

    assert await reliability.wait_for_db(connect=connect, sleep=sleep) == 3
    assert waits == [2.0, 4.0, 8.0]


async def test_wait_for_db_gives_up_after_its_limit():
    async def connect():
        raise TimeoutError

    async def sleep(_s):
        pass

    with pytest.raises(TimeoutError):
        await reliability.wait_for_db(60, connect=connect, sleep=sleep)


# ------------------------------------------------------------------ why late


def _hist(sleeps=(), starts=()):
    return {"sleeps": [[a.isoformat(), b.isoformat()] for a, b in sleeps], "starts": [s.isoformat() for s in starts]}


def test_late_reason_names_the_sleep():
    hist = _hist(sleeps=[(_local(19, 7), _local(22, 6))])
    assert reliability._explain(_local(20), _local(22, 9), hist) == "the laptop was asleep from 19:07 to 22:06"


def test_late_reason_names_the_restart():
    hist = _hist(starts=[_local(9), _local(22, 7)])
    assert reliability._explain(_local(20), _local(22, 9), hist) == (
        "Atlas was not running at 20:00 (the worker started at 22:07)"
    )


def test_late_reason_admits_when_it_does_not_know():
    assert reliability._explain(_local(20), _local(22, 9), _hist()) is None


async def test_history_keeps_starts_and_sleeps(monkeypatch):
    async with livedb.shadow(monkeypatch, "system_heartbeats"):
        await reliability.record_start(_local(22, 7).astimezone(UTC))
        await reliability.record_sleep(_local(19, 7).astimezone(UTC), _local(22, 6).astimezone(UTC))
        reason = await reliability.late_reason(_local(20), _local(22, 9))
    assert reason == "the laptop was asleep from 19:07 to 22:06"


# ------------------------------------------------------------------ catch-up with fake clocks


async def test_catch_up_runs_once_with_the_reason(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=False)),
        patch("core.reliability.late_reason", new=AsyncMock(return_value="the laptop was asleep from 19:07 to 22:06")),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        assert await briefs.catch_up_briefs(_local(22, 9)) == "evening"
        # The ten-minute tick after it must not run the brief a second time.
        assert await briefs.catch_up_briefs(_local(22, 19)) is None
    run.assert_awaited_once_with("evening", late="20:00", reason="the laptop was asleep from 19:07 to 22:06")


async def test_catch_up_leaves_the_slot_to_its_cron_job_for_five_minutes(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=False)),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        assert await briefs.catch_up_briefs(_local(20, 3)) is None
    run.assert_not_awaited()


async def test_the_cron_job_and_the_catch_up_never_both_run(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=False)),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        await briefs._scheduled("evening", _local(20, 0))
        assert await briefs.catch_up_briefs(_local(20, 10)) is None
        await briefs._scheduled("evening", _local(20, 30))  # a coalesced misfire
    run.assert_awaited_once_with("evening")


async def test_a_cron_run_that_fires_late_after_a_wake_says_so(slots):
    with (
        patch("core.reliability.late_reason", new=AsyncMock(return_value="the laptop was asleep from 19:40 to 20:35")),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        await briefs._scheduled("evening", _local(20, 36))
    run.assert_awaited_once_with("evening", late="20:00", reason="the laptop was asleep from 19:40 to 20:35")


def test_the_late_line_carries_the_reason():
    prompt = briefs._brief_prompt("evening", "20:00", "the laptop was asleep from 19:07 to 22:06")
    assert "it was due at 20:00 and the laptop was asleep from 19:07 to 22:06" in prompt


def test_scheduler_fires_the_briefs_and_the_checks_at_their_times(slots):
    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    now = _local(19, 59)
    assert jobs["evening_brief"].trigger.get_next_fire_time(None, now) == _local(20)
    assert jobs["morning_brief"].trigger.get_next_fire_time(None, now) == _local(8, day=21)
    assert jobs["slo_self_check"].trigger.get_next_fire_time(None, now) == _local(23, 50)
    trigger = jobs["brief_catch_up_tick"].trigger
    later = trigger.start_date + timedelta(hours=3, minutes=1)  # a wake three hours on
    tick = trigger.get_next_fire_time(None, later)
    assert tick is not None and timedelta(0) <= tick - later <= timedelta(minutes=10)
    assert jobs["brief_catch_up_tick"].max_instances == 1


# ------------------------------------------------------------------ the SLO


def test_the_check_judges_yesterday_when_it_runs_after_a_night_asleep():
    assert reliability.check_day(_local(23, 50)) == _local(0).date()
    assert reliability.check_day(_local(5, 30, day=21)) == _local(0).date()


async def _set_beat(kind: str, at: datetime, meta: dict) -> None:
    from core import db

    await db.execute(
        "insert into system_heartbeats (kind, last_at, metadata) values ($1, $2, $3) "
        "on conflict (kind) do update set last_at = excluded.last_at, metadata = excluded.metadata",
        kind, at, meta,
    )


async def test_tick_gap_keeps_the_worst_wait_of_the_day(monkeypatch):
    async with livedb.shadow(monkeypatch, "system_heartbeats"):
        assert (await reliability.tick_gap(_local(9)))["gaps"]["max_gap_min"] == 0.0
        await _set_beat("loop/executive", _local(9), {"gaps": {"day": "2026-10-20", "max_gap_min": 31.0}})
        assert (await reliability.tick_gap(_local(12)))["gaps"] == {"day": "2026-10-20", "max_gap_min": 180.0}
        await _set_beat("loop/executive", _local(12), {"gaps": {"day": "2026-10-20", "max_gap_min": 180.0}})
        assert (await reliability.tick_gap(_local(12, 30)))["gaps"]["max_gap_min"] == 180.0
        # A new day starts its own count.
        await _set_beat("loop/executive", _local(23, 50), {"gaps": {"day": "2026-10-20", "max_gap_min": 180.0}})
        assert (await reliability.tick_gap(_local(0, 20, day=21)))["gaps"] == {"day": "2026-10-21", "max_gap_min": 30.0}


async def test_self_check_records_the_night_of_2026_10_20(monkeypatch, slots):
    from core import db

    async with livedb.shadow(monkeypatch, "briefs", "slo_checks", "system_heartbeats"):
        await db.execute("insert into briefs (kind, body, created_at) values ('morning', 'm', $1)", _local(8, 2))
        await db.execute("insert into briefs (kind, body, created_at) values ('evening', 'e', $1)", _local(22, 10))
        await reliability.record_sleep(_local(19, 7).astimezone(UTC), _local(22, 6).astimezone(UTC))
        await _set_beat("loop/executive", _local(23, 40), {"gaps": {"day": "2026-10-20", "max_gap_min": 182.0}})

        results = {r["objective"]: r for r in await reliability.run_self_check(_local(23, 50))}
        assert results["brief/morning"]["ok"] is True and results["brief/morning"]["value"] == 2.0
        assert results["brief/evening"]["ok"] is False
        assert "asleep from 19:07 to 22:06" in results["brief/evening"]["detail"]
        assert results["heartbeat"]["ok"] is False and results["heartbeat"]["target"] == stale_after_min()
        assert "slept from 19:07 to 22:06" in results["heartbeat"]["detail"]

        # Run again: the same day's rows are replaced, not duplicated.
        await reliability.run_self_check(_local(23, 55))
        assert await db.fetchval("select count(*) from slo_checks") == 3

        summary = await reliability.slo_summary(days=3650)
        by = {o["objective"]: o for o in summary["objectives"]}
        assert by["brief/morning"]["passed"] == 1 and by["brief/evening"]["passed"] == 0
        assert summary["ok"] is False


async def test_a_missing_brief_fails_the_slo_and_says_why(monkeypatch, slots):
    async with livedb.shadow(monkeypatch, "briefs", "slo_checks", "system_heartbeats"):
        await reliability.record_start(_local(21, 0).astimezone(UTC))
        results = {r["objective"]: r for r in await reliability.run_self_check(_local(23, 50))}
    assert results["brief/evening"]["ok"] is False and results["brief/evening"]["value"] is None
    assert "not running at 20:00" in results["brief/evening"]["detail"]
    assert results["heartbeat"]["detail"] == "The background loop has never ticked."


async def test_a_slot_not_yet_due_is_not_judged(monkeypatch, slots):
    async with livedb.shadow(monkeypatch, "briefs", "slo_checks", "system_heartbeats"):
        results = await reliability.run_self_check(_local(19, 0))
    assert {r["objective"] for r in results} == {"brief/morning", "heartbeat"}


# ------------------------------------------------------------------ code on disk


def test_code_revision_reads_a_checkout_and_a_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (repo / ".git" / "refs" / "heads" / "master").write_text("159029a" + "0" * 33 + "\n", encoding="utf-8")
    assert reliability.code_revision(repo) == "159029a00000"

    # A worktree: .git is a file, its branch lives only in packed-refs.
    wt_git = repo / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/r12/reliability\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    (repo / ".git" / "packed-refs").write_text("abcdef1234567890 refs/heads/r12/reliability\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert reliability.code_revision(wt) == "abcdef123456"

    assert reliability.code_revision(tmp_path / "nowhere") is None


def test_a_suspend_is_not_a_blocked_loop():
    """On this laptop's standby the awake clock keeps counting, so the watch
    thread times its own sleep: a 60 s sleep that took 40 minutes was a suspend."""
    assert liveness.suspended(60.0, 2400.0)
    assert not liveness.suspended(60.0, 61.0), "a blocked event loop does not slow this thread"
    assert not liveness.suspended(60.0, 60.0 * liveness.SUSPEND_FACTOR)


def test_the_watch_thread_forgives_a_suspend_and_still_catches_a_block(monkeypatch):
    """Woken from a suspend, the switch resets its clocks instead of dying;
    a real block (normal sleeps, a quiet pulse) still ends the worker."""
    deaths: list[str] = []
    walls = iter([0.0, 3000.0, 3000.0, 3060.0])  # first sleep: 50 min suspend; second: normal
    monkeypatch.setattr(liveness.time, "sleep", lambda s: None)
    monkeypatch.setattr(liveness.time, "time", lambda: next(walls))
    awake = {"now": 10_000.0}
    monkeypatch.setattr(liveness, "_awake", lambda: awake["now"])

    def die(reason: str) -> None:
        deaths.append(reason)
        raise SystemExit

    monkeypatch.setattr(liveness, "_die", die)
    with patch.dict(liveness._state, {"pulse": 10_000.0 - 3000.0, "cycle": 10_000.0}):
        # Suspend: the pulse looks 50 min quiet, but the thread's own sleep ran long.
        # After the reset the next round sees a pulse 20 min quiet: a real block.
        def advance(s: float) -> None:
            awake["now"] += 1200.0

        monkeypatch.setattr(liveness.time, "sleep", advance)
        with pytest.raises(SystemExit):
            liveness._watch(60.0)
    assert len(deaths) == 1 and deaths[0].startswith("event loop blocked for 1200 s")


# ------------------------------------------------------------------ 2026-10-22: a failed check, a hung catch-up


async def test_a_failed_self_check_tries_again(monkeypatch):
    calls = []

    async def flaky(now=None):
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("the specified network name is no longer available")
        return []

    monkeypatch.setattr(reliability, "run_self_check", flaky)
    await reliability.scheduled_self_check(wait_s=0)
    assert len(calls) == 2


async def test_a_day_with_no_verdict_is_judged_the_next_morning(monkeypatch, slots):
    """The 23:50 check failed on 2026-10-22 and the evening brief never went
    out: the day must read as a miss, not "0 of 0"."""
    from core import db

    reliability._SELF_CHECKED.clear()
    async with livedb.shadow(monkeypatch, "briefs", "slo_checks", "system_heartbeats"):
        assert await reliability.catch_up_self_check(_local(0, 41, day=23)) is not None
        rows = {r["objective"]: r["ok"] for r in await db.fetch("select objective, ok from slo_checks where day = '2026-10-22'")}
        assert rows["brief/evening"] is False and rows["brief/morning"] is False
        assert await reliability.catch_up_self_check(_local(0, 51, day=23)) is None, "once a day"
        reliability._SELF_CHECKED.clear()
        assert await reliability.catch_up_self_check(_local(1, 1, day=23)) is None, "the day has its verdict"
    reliability._SELF_CHECKED.clear()


async def test_a_hung_catch_up_gives_up_and_the_rest_still_run(monkeypatch):
    import asyncio

    async def hang():
        await asyncio.sleep(3600)

    ran = []

    async def check():
        ran.append("check")

    async def discovery():
        ran.append("discovery")

    monkeypatch.setattr(briefs, "catch_up_briefs", hang)
    monkeypatch.setattr(briefs, "catch_up_discovery", discovery)
    monkeypatch.setattr(reliability, "catch_up_self_check", check)
    monkeypatch.setattr(briefs, "CATCH_UP_BRIEF_LIMIT_S", 0.05)
    await briefs.catch_up_tick()
    assert ran == ["check", "discovery"]
