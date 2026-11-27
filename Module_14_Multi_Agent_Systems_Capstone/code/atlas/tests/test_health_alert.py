"""Unit tests for the proactive health-alerting layer.

Heartbeats and the alert job are exercised with db.fetch / db.fetchval /
db.execute mocked, and with the Telegram bot stubbed, so no live DB or
real Telegram traffic happens.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import core.loop.health as health_mod
from core.heartbeats import all_heartbeats, get_heartbeat, set_heartbeat
from core.loop.health import (
    _ALERT_COOLDOWN_S,
    _format_alert,
    health_alert_job,
    health_check,
)

# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------


async def test_set_heartbeat_upserts():
    with patch("core.heartbeats.db.execute", new=AsyncMock()) as ex:
        await set_heartbeat("loop/executive", {"triggers_fired": 0})
    # The query is an INSERT ... ON CONFLICT upsert.
    sent_query = ex.call_args.args[0]
    assert "insert into system_heartbeats" in sent_query
    assert "on conflict" in sent_query
    assert ex.call_args.args[1] == "loop/executive"


async def test_get_heartbeat_returns_timestamp():
    when = datetime.now(UTC)
    with patch(
        "core.heartbeats.db.fetchrow",
        new=AsyncMock(return_value={"last_at": when}),
    ):
        out = await get_heartbeat("loop/executive")
    assert out == when


async def test_get_heartbeat_missing_returns_none():
    with patch(
        "core.heartbeats.db.fetchrow", new=AsyncMock(return_value=None)
    ):
        assert await get_heartbeat("loop/executive") is None


async def test_all_heartbeats_indexed_by_kind():
    rows = [
        {"kind": "loop/executive", "last_at": datetime(2026, 5, 26)},
        {"kind": "brief/morning", "last_at": datetime(2026, 5, 26, 8)},
    ]
    with patch("core.heartbeats.db.fetch", new=AsyncMock(return_value=rows)):
        out = await all_heartbeats()
    assert set(out.keys()) == {"loop/executive", "brief/morning"}


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


async def test_health_check_all_clear():
    now = datetime.now(UTC)
    beats = {"loop/executive": now - timedelta(minutes=10)}
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value=beats)
    ), patch("core.loop.health.db.fetchval", new=AsyncMock(return_value=1)):
        issues = await health_check()
    assert issues == []


async def test_health_check_flags_stale_executive_loop():
    now = datetime.now(UTC)
    # 4 hours stale, well beyond 3 * 30min default.
    beats = {"loop/executive": now - timedelta(hours=4)}
    # Pre-empt the startup grace: pretend the process has been up for an
    # hour so the executive loop alert is in play.
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value=beats)
    ), patch(
        "core.loop.health.db.fetchval", new=AsyncMock(return_value=1)
    ):
        issues = await health_check()
    assert len(issues) == 1
    assert issues[0]["kind"] == "loop/executive"
    assert "has not run for 240 minutes" in issues[0]["detail"]


async def test_health_check_flags_db_unreachable():
    now = datetime.now(UTC)
    beats = {"loop/executive": now - timedelta(minutes=10)}
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value=beats)
    ), patch(
        "core.loop.health.db.fetchval",
        new=AsyncMock(side_effect=ConnectionError("connection refused")),
    ):
        issues = await health_check()
    assert any(i["kind"] == "db/connection" for i in issues)


async def test_health_check_flags_db_heartbeats_when_unreadable():
    """If all_heartbeats itself raises, db is the issue."""
    with patch(
        "core.loop.health.all_heartbeats",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        issues = await health_check()
    assert len(issues) == 1
    assert issues[0]["kind"] == "db/heartbeats"


async def test_health_check_no_heartbeats_yet_is_silent():
    """A fresh install with no heartbeats yet is not an issue."""
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value={})
    ), patch("core.loop.health.db.fetchval", new=AsyncMock(return_value=1)):
        issues = await health_check()
    assert issues == []


# ---------------------------------------------------------------------------
# Alert formatting and cooldown
# ---------------------------------------------------------------------------


def test_format_alert_lists_issues():
    text = _format_alert(
        [
            {"kind": "loop/executive", "detail": "loop hasn't ticked in 45m."},
            {"kind": "db/connection", "detail": "postgres unreachable."},
        ]
    )
    assert "Atlas health alert" in text
    assert "loop hasn't ticked in 45m." in text
    assert "postgres unreachable." in text


def _reset_alert_state():
    health_mod._LAST_ALERTED.clear()
    health_mod._OPEN.clear()
    health_mod._SEEN.clear()


async def test_health_alert_job_silent_when_clear():
    _reset_alert_state()
    with patch("core.loop.health.health_check", new=AsyncMock(return_value=[])):
        await health_alert_job()  # no exception, no need to assert send_message


async def test_health_alert_job_sends_when_issue_found():
    _reset_alert_state()
    issues = [{"kind": "loop/executive", "detail": "loop quiet."}]
    note = AsyncMock()
    with patch(
        "core.loop.health.health_check", new=AsyncMock(return_value=issues)
    ), patch("core.surfaces.notify.notify", new=note):
        await health_alert_job()  # first sighting: could be a wake-time race, no page
        note.assert_not_awaited()
        await health_alert_job()  # still there five minutes later: page
    note.assert_awaited_once()
    assert note.await_args.kwargs["kind"] == "health"
    assert note.await_args.kwargs["priority"] == "high"
    assert "loop quiet." in note.await_args.kwargs["body"]


async def test_health_alert_job_dedups_within_cooldown():
    _reset_alert_state()
    issues = [{"kind": "loop/executive", "detail": "still quiet."}]
    note = AsyncMock()
    with patch(
        "core.loop.health.health_check", new=AsyncMock(return_value=issues)
    ), patch("core.surfaces.notify.notify", new=note):
        await health_alert_job()  # first sighting
        await health_alert_job()  # confirmed: alerts
        await health_alert_job()  # cooldown silences this one
    assert note.await_count == 1


async def test_health_alert_job_alerts_again_after_cooldown():
    _reset_alert_state()
    issues = [{"kind": "loop/executive", "detail": "still quiet."}]
    note = AsyncMock()
    with patch(
        "core.loop.health.health_check", new=AsyncMock(return_value=issues)
    ), patch("core.surfaces.notify.notify", new=note):
        await health_alert_job()
        await health_alert_job()
        # Roll the in-memory cooldown back so the next call is "fresh".
        for k in list(health_mod._LAST_ALERTED):
            health_mod._LAST_ALERTED[k] = time.time() - _ALERT_COOLDOWN_S - 1
        await health_alert_job()
    assert note.await_count == 2


async def test_a_blip_seen_once_pages_nobody():
    """On a wake the watchdog can read the old tick a second before the worker's
    catch-up tick lands. One pass with the issue, the next without: no page."""
    _reset_alert_state()
    note = AsyncMock()
    check = AsyncMock(side_effect=[[{"kind": "loop/executive", "detail": "quiet."}], [],
                                   [{"kind": "loop/executive", "detail": "quiet."}]])
    with patch("core.loop.health.health_check", new=check), patch("core.surfaces.notify.notify", new=note):
        for _ in range(3):
            await health_alert_job()
    note.assert_not_awaited()


async def test_health_alert_job_swallows_health_check_failure():
    """If health_check raises, the job must not crash the scheduler."""
    _reset_alert_state()
    with patch(
        "core.loop.health.health_check",
        new=AsyncMock(side_effect=RuntimeError("explode")),
    ):
        await health_alert_job()  # no raise


def test_a_restart_is_judged_by_the_new_worker_not_the_old_tick():
    """The audit saw a 40-minute-old tick from the previous process called fresh
    after a restart. The current worker's own first tick is what counts."""
    from datetime import UTC, datetime, timedelta

    from core.loop.health import FIRST_TICK_MIN, worker_status

    now = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
    old_tick = now - timedelta(minutes=40)
    just_started = worker_status({"loop/executive": old_tick, "worker/started": now - timedelta(minutes=2)}, now)
    assert (just_started["state"], just_started["fresh"], just_started["ticked_since_start"]) == ("starting", True, False)
    never_ticked = worker_status({"loop/executive": old_tick,
                                  "worker/started": now - timedelta(minutes=FIRST_TICK_MIN + 1)}, now)
    assert (never_ticked["state"], never_ticked["fresh"]) == ("stalled", False)
    ticked = worker_status({"loop/executive": now - timedelta(minutes=1),
                            "worker/started": now - timedelta(minutes=2)}, now)
    assert (ticked["state"], ticked["fresh"], ticked["ticked_since_start"]) == ("ticking", True, True)
    quiet = worker_status({"loop/executive": now - timedelta(minutes=70),
                           "worker/started": now - timedelta(hours=5)}, now)
    assert quiet["state"] == "stalled"


def test_the_loop_ticks_a_minute_after_the_worker_starts():
    from datetime import datetime

    from core.loop.scheduler import build_scheduler

    sched = build_scheduler()
    job = sched.get_job("executive_loop")
    first = job.next_run_time if hasattr(job, "next_run_time") else job.trigger.get_next_fire_time(None, datetime.now(job.trigger.timezone))
    assert first is not None
    assert (first - datetime.now(first.tzinfo)).total_seconds() < 120, "not a full interval later"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))



async def test_health_check_quiet_while_a_restarted_worker_starts():
    """A worker that just restarted has not ticked yet; its worker/started
    heartbeat, newer than the stale tick, keeps the alert quiet."""
    now = datetime.now(UTC)
    beats = {
        "loop/executive": now - timedelta(hours=6),
        "worker/started": now - timedelta(minutes=5),
    }
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value=beats)
    ), patch(
        "core.loop.health.db.fetchval", new=AsyncMock(return_value=1)
    ):
        issues = await health_check()
    assert not any(i["kind"] == "loop/executive" for i in issues)


async def test_health_check_alerts_after_startup_grace_expires():
    """Past the grace window, the same stale heartbeat does page."""
    now = datetime.now(UTC)
    beats = {"loop/executive": now - timedelta(hours=6)}
    # Process started long ago: well past the 20-minute grace.
    with patch(
        "core.loop.health.all_heartbeats", new=AsyncMock(return_value=beats)
    ), patch(
        "core.loop.health.db.fetchval", new=AsyncMock(return_value=1)
    ):
        issues = await health_check()
    assert any(i["kind"] == "loop/executive" for i in issues)
