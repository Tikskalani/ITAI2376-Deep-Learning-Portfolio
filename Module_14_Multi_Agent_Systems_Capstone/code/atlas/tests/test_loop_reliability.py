"""The loop must notice its own death, recover, and say what it missed.

Covers the 2026-10-08 audit findings: a stall the worker could not report,
a health check that read ok with a dead loop, briefs lost to one network
blip or to a worker that started after the slot, and reminders that fired
weeks late looking on time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest

import core.loop.health as health_mod
from core import liveness
from core.config import settings
from core.llm import is_transient
from core.loop import briefs, executive
from core.loop.health import health_alert_job, worker_status

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ worker status


@pytest.mark.parametrize(
    "beats,state,fresh",
    [
        ({}, "never", False),
        ({"loop/executive": NOW - timedelta(minutes=10)}, "ticking", True),
        ({"loop/executive": NOW - timedelta(minutes=108)}, "stalled", False),
        ({"loop/executive": NOW - timedelta(hours=9), "worker/started": NOW - timedelta(minutes=3)}, "starting", True),
        ({"worker/started": NOW - timedelta(minutes=3)}, "starting", True),
        ({"worker/started": NOW - timedelta(hours=3)}, "stalled", False),
        # Started before the last tick: the tick decides.
        ({"loop/executive": NOW - timedelta(hours=2), "worker/started": NOW - timedelta(hours=3)}, "stalled", False),
    ],
)
def test_worker_status(beats, state, fresh):
    s = worker_status(beats, NOW)
    assert (s["state"], s["fresh"]) == (state, fresh)


# ------------------------------------------------------------------ alert job


@pytest.fixture(autouse=True)
def _reset_alerts():
    health_mod._LAST_ALERTED.clear()
    health_mod._OPEN.clear()
    health_mod._SEEN.clear()
    yield
    health_mod._LAST_ALERTED.clear()
    health_mod._OPEN.clear()
    health_mod._SEEN.clear()


async def test_alert_title_says_what_broke_and_recovery_is_announced():
    issue = [{"kind": "loop/executive", "detail": "The background loop has not run for 45 minutes."}]
    note = AsyncMock()
    with patch("core.surfaces.notify.notify", new=note):
        with patch("core.loop.health.health_check", new=AsyncMock(return_value=issue)):
            await health_alert_job()  # first sighting: no page (could be a wake-time race)
            await health_alert_job()  # confirmed on the next pass: page
        assert note.await_args.kwargs["title"] == "Atlas loop stalled"
        assert "45 minutes" in note.await_args.kwargs["body"]
        with patch("core.loop.health.health_check", new=AsyncMock(return_value=[])):
            await health_alert_job()
            await health_alert_job()  # recovery is said once
    assert note.await_count == 2
    assert note.await_args.kwargs["title"] == "Atlas recovered"
    assert note.await_args.kwargs["priority"] == "normal"


async def test_watchdog_survives_a_failing_pass():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db blip")
        raise asyncio.CancelledError

    with patch("core.loop.health.health_alert_job", new=flaky):
        with pytest.raises(asyncio.CancelledError):
            await health_mod.watch_forever(interval_s=0, first_delay_s=0)
    assert calls == 2


# ------------------------------------------------------------------ liveness


def test_liveness_flags_a_blocked_event_loop_and_a_stuck_cycle():
    t0 = 1000.0
    with patch.dict(liveness._state, {"pulse": t0, "cycle": t0}):
        assert liveness.check(t0 + 60) is None
        assert "event loop blocked" in liveness.check(t0 + liveness.PULSE_LIMIT_S + 1)
        liveness._state["pulse"] = t0 + liveness.cycle_limit_s()
        assert "no executive tick" in liveness.check(t0 + liveness.cycle_limit_s() + 1)
        liveness.cycle_done()
        liveness.pulse()
        assert liveness.check() is None


# ------------------------------------------------------------------ loop timeouts


async def test_a_hung_senses_stage_is_cut_off_and_the_tick_completes(monkeypatch):
    monkeypatch.setattr(settings, "loop_stage_timeout_min", 0.001)

    async def hang(_deliver):
        await asyncio.sleep(30)

    beat = AsyncMock()
    with (
        patch("core.loop.executive.get_due_triggers", new=AsyncMock(return_value=[])),
        patch("core.loop.executive.run_senses_cycle", new=hang),
        patch("core.loop.executive.list_goals", new=AsyncMock(return_value=[])),
        patch("core.loop.executive.set_heartbeat", new=beat),
        patch("core.loop.executive.ping_external_heartbeat", new=AsyncMock()),
    ):
        summary = await asyncio.wait_for(executive.run_executive_cycle(), timeout=5)
    assert summary["signals_triaged"] == 0
    beat.assert_awaited_once()


# ------------------------------------------------------------------ overdue triggers


def _trigger(**over):
    base = {"id": uuid4(), "payload": {}, "description": "a trigger", "fires_at": None}
    base.update(over)
    return base


def test_overdue_phrase():
    on_time = _trigger(fires_at=datetime.now(UTC) - timedelta(minutes=5))
    assert executive._overdue(on_time) == ""
    late = _trigger(fires_at=datetime(2026, 8, 30, 14, 0, tzinfo=UTC))
    phrase = executive._overdue(late, now=datetime(2026, 10, 8, 14, 0, tzinfo=UTC))
    assert phrase.startswith("was due ") and "Aug 30" in phrase and "(39 days ago)" in phrase


async def test_a_late_reminder_says_it_is_overdue():
    trig = _trigger(
        payload={"message": "Renew auto insurance"},
        fires_at=datetime.now(UTC) - timedelta(days=39),
    )
    note = AsyncMock()
    with (
        patch("core.loop.executive.notify", new=note),
        patch("core.loop.executive.mark_fired", new=AsyncMock()) as mark,
    ):
        await executive._fire_trigger(trig)
    kw = note.await_args.kwargs
    assert kw["title"] == "Overdue: Renew auto insurance"
    assert "39 days ago" in kw["body"] and kw["priority"] == "high"
    assert kw["data"]["overdue"] is True
    mark.assert_awaited_once()


async def test_a_late_agent_task_is_told_it_is_late():
    trig = _trigger(payload={"agent": "cfo", "task": "Check the card statement"},
                    fires_at=datetime.now(UTC) - timedelta(days=3))
    disp = AsyncMock()
    with (
        patch("core.loop.executive.dispatch", new=disp),
        patch("core.loop.executive.mark_fired", new=AsyncMock()),
    ):
        await executive._fire_trigger(trig)
    task = disp.await_args.args[1]
    assert task.startswith("[Scheduled task, running late") and task.endswith("Check the card statement")


# ------------------------------------------------------------------ brief retries


def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def test_is_transient():
    assert is_transient(_conn_error())
    assert is_transient(httpx.ConnectError("no route"))
    assert not is_transient(RuntimeError("bug"))
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com"))
    assert not is_transient(anthropic.BadRequestError("credit balance is too low", response=resp, body=None))
    resp = httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com"))
    assert is_transient(anthropic.APIStatusError("overloaded", response=resp, body=None))


async def test_brief_retries_a_network_blip_then_delivers(monkeypatch):
    monkeypatch.setattr(briefs, "RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    disp = AsyncMock(side_effect=[_conn_error(), _conn_error(), SimpleNamespace(text="Morning.")])
    store = AsyncMock(return_value=uuid4())
    with (
        patch("core.loop.briefs.dispatch", new=disp),
        patch("core.loop.briefs.notify", new=AsyncMock(return_value=None)),
        patch("core.loop.briefs._store_brief", new=store),
        patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="x")),
        patch("core.loop.briefs.set_heartbeat", new=AsyncMock()),
    ):
        await briefs.run_brief("morning")
    assert disp.await_count == 3
    store.assert_awaited_once_with("morning", "Morning.")


async def test_brief_gives_up_after_the_retries_and_says_why(monkeypatch):
    monkeypatch.setattr(briefs, "RETRY_DELAYS_S", (0.0, 0.0))
    note = AsyncMock(return_value=None)
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(side_effect=_conn_error())) as disp,
        patch("core.loop.briefs.notify", new=note),
        patch("core.loop.briefs._store_brief", new=AsyncMock()) as store,
        patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="x")),
    ):
        await briefs.run_brief("evening")
    assert disp.await_count == 3
    store.assert_not_awaited()
    body = note.await_args.kwargs["body"]
    assert "after retrying" in body and "internet connection" in body


# ------------------------------------------------------------------ brief catch-up


def _local(h, m=0):
    return datetime(2026, 10, 8, h, m, tzinfo=ZoneInfo(settings.timezone))


@pytest.fixture
def slots(monkeypatch):
    monkeypatch.setattr(settings, "brief_morning", time(8, 0))
    monkeypatch.setattr(settings, "brief_evening", time(20, 0))
    briefs._ATTEMPTED.clear()
    # Why a brief is late comes from the worker history; these tests pin it.
    monkeypatch.setattr("core.reliability.late_reason", AsyncMock(return_value="Atlas was not running then"))


async def test_catch_up_does_nothing_before_the_morning_slot(slots):
    with patch("core.loop.briefs.run_brief", new=AsyncMock()) as run:
        assert await briefs.catch_up_briefs(_local(7, 30)) is None
    run.assert_not_awaited()


async def test_catch_up_runs_a_missed_morning_brief_marked_late(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=False)),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        assert await briefs.catch_up_briefs(_local(10, 15)) == "morning"
    run.assert_awaited_once_with("morning", late="08:00", reason="Atlas was not running then")


async def test_catch_up_skips_a_brief_already_sent(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=True)),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        assert await briefs.catch_up_briefs(_local(21)) is None
    run.assert_not_awaited()


async def test_catch_up_prefers_the_latest_slot(slots):
    with (
        patch("core.loop.briefs._brief_today", new=AsyncMock(return_value=False)),
        patch("core.loop.briefs.run_brief", new=AsyncMock()) as run,
    ):
        assert await briefs.catch_up_briefs(_local(21)) == "evening"
    run.assert_awaited_once_with("evening", late="20:00", reason="Atlas was not running then")


def test_late_brief_prompt_says_so():
    assert "running late: it was due at 08:00" in briefs._brief_prompt("morning", "08:00")
    assert "late" not in briefs._brief_prompt("morning").split(".")[0]


# ------------------------------------------------------------------ decision reviews


async def test_overdue_reviews_are_sent_when_none_went_out_this_week():
    with (
        patch("core.loop.briefs.db.fetchval", new=AsyncMock(return_value=False)),
        patch("core.loop.briefs.send_review_round", new=AsyncMock(return_value=2)) as rnd,
    ):
        assert await briefs.catch_up_decision_reviews() == 2
    rnd.assert_awaited_once()
    with (
        patch("core.loop.briefs.db.fetchval", new=AsyncMock(return_value=True)),
        patch("core.loop.briefs.send_review_round", new=AsyncMock()) as rnd,
    ):
        assert await briefs.catch_up_decision_reviews() == 0
    rnd.assert_not_awaited()


async def test_review_round_runs_even_when_the_weekly_narrative_fails(monkeypatch):
    monkeypatch.setattr(briefs, "RETRY_DELAYS_S", ())
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(side_effect=RuntimeError("bug"))),
        patch("core.loop.briefs.notify", new=AsyncMock(return_value=None)),
        patch("core.loop.briefs._weekly_context", new=AsyncMock(return_value="ctx")),
        patch("core.loop.briefs.send_review_round", new=AsyncMock(return_value=1)) as rnd,
    ):
        await briefs.run_weekly_review()
    rnd.assert_awaited_once()


@pytest.fixture(autouse=True)
def _no_owed_promises(monkeypatch):
    # These tests compare the delivered text exactly; the promises appended
    # from config/context.yaml are covered by tests/test_owed_promises.py.
    monkeypatch.setattr("core.loop.briefs._owes", lambda: "")
