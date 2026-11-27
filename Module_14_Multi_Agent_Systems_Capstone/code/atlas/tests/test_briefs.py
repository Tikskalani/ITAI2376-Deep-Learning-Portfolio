"""Unit tests for the loop layer: briefs, weekly review, executive cycle,
memory hygiene, and the scheduler.

Everything external is mocked: no Claude API, no Postgres, no push.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from core.loop import briefs, executive, hygiene
from core.loop.scheduler import build_scheduler
from tests import livedb


@pytest_asyncio.fixture(autouse=True)
async def _private_heartbeats(monkeypatch):
    # Briefs and the loop tick their heartbeats; a test must not make the real
    # ones look fresh, or the health check would miss a stalled loop.
    async with livedb.shadow(monkeypatch, "system_heartbeats"):
        yield


def _patch_brief_delivery(note: AsyncMock):
    """Patch the brief's delivery path: notify, the briefs row, the signals block."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("core.loop.briefs.notify", new=note))
    stack.enter_context(patch("core.loop.briefs._store_brief", new=AsyncMock(return_value=uuid4())))
    stack.enter_context(patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="- [dental] 08:10 !PR #7 waits on review (github)")))
    stack.enter_context(patch("core.loop.briefs.db.execute", new=AsyncMock()))
    return stack


def _trigger(**over):
    """A due-trigger row shaped like get_due_triggers returns."""
    base = {
        "id": uuid4(),
        "trigger_type": "time",
        "spec": {},
        "payload": {},
        "status": "active",
        "description": "a trigger",
        "created_at": None,
        "fires_at": None,
        "fired_at": None,
        "fire_count": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------


async def test_run_brief_morning_dispatches_with_signals_and_delivers():
    note = AsyncMock(return_value=SimpleNamespace(notification_id=uuid4()))
    result = SimpleNamespace(text="Here is your morning brief.")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)) as disp,
        _patch_brief_delivery(note),
    ):
        await briefs.run_brief("morning")
    disp.assert_awaited_once()
    assert disp.await_args.args[0] == "ceo"
    assert "lead with it" in disp.await_args.args[1]
    assert "at most 5 items" in disp.await_args.args[1]
    context = disp.await_args.kwargs["memory_context"]
    assert context.startswith("What arrived in the last 24 hours:")
    assert "PR #7 waits on review" in context
    note.assert_awaited_once()
    kw = note.await_args.kwargs
    assert kw["kind"] == "brief/morning" and kw["body"] == "Here is your morning brief."
    assert kw["title"].startswith("Morning brief, ")
    assert "brief_id" in kw["data"] and kw["priority"] == "normal"


async def test_run_brief_evening_cue_mentions_evening_and_uses_shorter_window():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="Evening brief.")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)) as disp,
        _patch_brief_delivery(note),
        patch("core.loop.briefs.daily_health_line", new=AsyncMock(return_value="health line")),
    ):
        await briefs.run_brief("evening")
    assert "evening" in disp.await_args.args[1].lower()
    assert "last 12 hours" in disp.await_args.kwargs["memory_context"]
    note.assert_awaited_once()
    assert note.await_args.kwargs["kind"] == "brief/evening"


async def test_run_brief_bad_kind_raises():
    with pytest.raises(ValueError):
        await briefs.run_brief("midnight")


async def test_run_brief_survives_dispatch_failure_and_does_not_store():
    note = AsyncMock(return_value=None)
    store = AsyncMock()
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(side_effect=RuntimeError("api down"))),
        patch("core.loop.briefs.notify", new=note),
        patch("core.loop.briefs._store_brief", new=store),
        patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="x")),
    ):
        await briefs.run_brief("morning")
    note.assert_awaited_once()
    body = note.await_args.kwargs["body"].lower()
    assert "could not write the morning brief" in body
    assert "retrying would not help" in body
    assert note.await_args.kwargs["priority"] == "high"
    store.assert_not_awaited()


async def test_run_brief_survives_signals_block_failure():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="Brief.")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)) as disp,
        patch("core.loop.briefs.notify", new=note),
        patch("core.loop.briefs._store_brief", new=AsyncMock(return_value=uuid4())),
        patch("core.loop.briefs.brief_block", new=AsyncMock(side_effect=RuntimeError("db"))),
    ):
        await briefs.run_brief("morning")
    assert "(signals unavailable)" in disp.await_args.kwargs["memory_context"]
    note.assert_awaited_once()


async def test_run_brief_empty_text_delivers_fallback():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="   ")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)),
        _patch_brief_delivery(note),
    ):
        await briefs.run_brief("morning")
    assert "empty" in note.await_args.kwargs["body"].lower()


async def test_run_brief_long_text_is_delivered_whole():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="x" * 9000)
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)),
        _patch_brief_delivery(note),
    ):
        await briefs.run_brief("morning")
    note.assert_awaited_once()
    assert len(note.await_args.kwargs["body"]) == 9000  # the inbox keeps the full brief


# ---------------------------------------------------------------------------
# Weekly review
# ---------------------------------------------------------------------------


def _patch_weekly(note: AsyncMock, *, review_round=None, dispatch=None, decisions=None):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("core.loop.briefs.dispatch", new=dispatch or AsyncMock(return_value=SimpleNamespace(text="Here is your week."))))
    stack.enter_context(patch("core.loop.briefs.list_decisions", new=AsyncMock(return_value=decisions or [])))
    stack.enter_context(patch("core.loop.briefs.decisions_due_for_review", new=AsyncMock(return_value=[])))
    stack.enter_context(patch("core.loop.briefs.send_review_round", new=review_round or AsyncMock(return_value=0)))
    stack.enter_context(_patch_brief_delivery(note))
    return stack


async def test_weekly_review_dispatches_with_signals_and_decision_context():
    note = AsyncMock(return_value=None)
    disp = AsyncMock(return_value=SimpleNamespace(text="Here is your week."))
    review_round = AsyncMock(return_value=0)
    with _patch_weekly(note, review_round=review_round, dispatch=disp,
                       decisions=[{"summary": "picked supplier A"}]):
        await briefs.run_weekly_review()
    disp.assert_awaited_once()
    context = disp.await_args.kwargs["memory_context"]
    assert "picked supplier A" in context
    assert "last 168 hours" in context
    note.assert_awaited_once()
    assert note.await_args.kwargs["kind"] == "brief/weekly"
    assert "Here is your week." in note.await_args.kwargs["body"]
    review_round.assert_awaited_once()


async def test_weekly_review_survives_dispatch_failure():
    note = AsyncMock(return_value=None)
    with _patch_weekly(note, dispatch=AsyncMock(side_effect=RuntimeError("api down"))):
        await briefs.run_weekly_review()
    note.assert_awaited_once()
    assert "could not" in note.await_args.kwargs["body"].lower()


async def test_weekly_review_runs_decision_review_round_after_delivery():
    note = AsyncMock(return_value=None)
    review_round = AsyncMock(return_value=2)
    with _patch_weekly(note, review_round=review_round):
        await briefs.run_weekly_review()
    review_round.assert_awaited_once()


async def test_weekly_review_survives_review_round_failure():
    """A failure in send_review_round must not break weekly-review delivery."""
    note = AsyncMock(return_value=None)
    with _patch_weekly(note, review_round=AsyncMock(side_effect=RuntimeError("boom"))):
        await briefs.run_weekly_review()
    note.assert_awaited_once()


# ---------------------------------------------------------------------------
# Executive loop
# ---------------------------------------------------------------------------


async def test_executive_cycle_no_triggers():
    with (
        patch(
            "core.loop.executive.get_due_triggers", new=AsyncMock(return_value=[])
        ),
        patch(
            "core.loop.executive.list_goals",
            new=AsyncMock(return_value=[object(), object()]),
        ),
    ):
        out = await executive.run_executive_cycle()
    assert out["triggers_fired"] == 0 and out["active_goals"] == 2
    # R2: the senses cycle reports into the same summary.
    assert {"signals_triaged", "interrupts", "prepared"} <= set(out)


async def test_executive_fires_agent_trigger():
    trig = _trigger(payload={"agent": "ceo", "task": "do x"})
    with (
        patch(
            "core.loop.executive.get_due_triggers",
            new=AsyncMock(return_value=[trig]),
        ),
        patch("core.loop.executive.mark_fired", new=AsyncMock()) as mark,
        patch("core.loop.executive.dispatch", new=AsyncMock()) as disp,
        patch("core.loop.executive.list_goals", new=AsyncMock(return_value=[])),
    ):
        out = await executive.run_executive_cycle()
    disp.assert_awaited_once()
    assert disp.await_args.args[0] == "ceo"
    mark.assert_awaited_once_with(trig["id"])
    assert out["triggers_fired"] == 1


async def test_executive_fires_message_trigger():
    trig = _trigger(payload={"message": "renew the ACA plan"})
    note = AsyncMock(return_value=None)
    with (
        patch(
            "core.loop.executive.get_due_triggers",
            new=AsyncMock(return_value=[trig]),
        ),
        patch("core.loop.executive.mark_fired", new=AsyncMock()) as mark,
        patch("core.loop.executive.list_goals", new=AsyncMock(return_value=[])),
        patch("core.loop.executive.run_senses_cycle", new=AsyncMock(return_value={})),
        patch("core.loop.executive.notify", new=note),
    ):
        await executive.run_executive_cycle()
    note.assert_awaited_once()
    assert note.await_args.kwargs["kind"] == "reminder"
    assert note.await_args.kwargs["title"] == "Reminder: renew the ACA plan"
    # The title says it; the body does not repeat it.
    assert "renew the ACA plan" not in note.await_args.kwargs["body"]
    mark.assert_awaited_once()


async def test_executive_marks_fired_even_when_firing_fails():
    trig = _trigger(payload={"agent": "ceo", "task": "x"})
    with (
        patch(
            "core.loop.executive.get_due_triggers",
            new=AsyncMock(return_value=[trig]),
        ),
        patch("core.loop.executive.mark_fired", new=AsyncMock()) as mark,
        patch(
            "core.loop.executive.dispatch",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("core.loop.executive.list_goals", new=AsyncMock(return_value=[])),
    ):
        out = await executive.run_executive_cycle()
    mark.assert_awaited_once()
    assert out["triggers_fired"] == 1


async def test_executive_survives_due_query_failure():
    with (
        patch(
            "core.loop.executive.get_due_triggers",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch("core.loop.executive.list_goals", new=AsyncMock(return_value=[])),
    ):
        out = await executive.run_executive_cycle()
    assert out["triggers_fired"] == 0


# ---------------------------------------------------------------------------
# Memory hygiene
# ---------------------------------------------------------------------------


async def test_hygiene_returns_summary():
    with (
        patch(
            "core.loop.hygiene.decisions_due_for_review",
            new=AsyncMock(return_value=[object(), object()]),
        ),
        patch("core.loop.hygiene.db.fetchval", new=AsyncMock(return_value=5)),
        patch(
            "core.loop.hygiene.merge_duplicate_entities",
            new=AsyncMock(return_value={"groups_merged": 1, "rows_merged": 2}),
        ),
    ):
        out = await hygiene.run_hygiene()
    assert out["decisions_due"] == 2
    assert out["stale_facts"] == 5
    assert out["groups_merged"] == 1
    assert out["rows_merged"] == 2


async def test_hygiene_survives_failures():
    with (
        patch(
            "core.loop.hygiene.decisions_due_for_review",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "core.loop.hygiene.db.fetchval",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "core.loop.hygiene.merge_duplicate_entities",
            new=AsyncMock(side_effect=RuntimeError("kg down")),
        ),
    ):
        out = await hygiene.run_hygiene()  # must not raise
    assert out["decisions_due"] == 0
    assert out["stale_facts"] == 0
    assert out["groups_merged"] == 0
    assert out["rows_merged"] == 0


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def test_build_scheduler_carries_all_jobs():
    scheduler = build_scheduler()
    job_ids = {j.id for j in scheduler.get_jobs()}
    assert job_ids == {
        "morning_brief",
        "evening_brief",
        "brief_catch_up_tick",
        "slo_self_check",
        "executive_loop",
        "weekly_review",
        "memory_hygiene",
        "daily_backup",
        "discovery_scan",
        "learning_digest",
        "weekly_learnings_review",
        "expire_approvals",
        "late_approvals",
        "kb_import",
        "env_reload",
        "web_watch",
        "email_unread_notifications",
        "workflow_tick",
        "memory_consolidation",
        "mail_open_loops",
        "news_brief",
        "recurring_tasks",
        "playbook_reflection",
        "mail_learning",
        "jobs_mail",
        "jobs_discovery",
        "jobs_queue",
        "jobs_submit",
        "night_propose",
        "night_run",
        "night_report",
        "jobs_daily",
    }


async def test_a_missed_discovery_night_is_made_up_once():
    """5 of 14 nights after Oct 8 had no discovery: the worker was asleep or
    waiting on Postgres at 05:15. The catch-up runs it (and the queue) once."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Chicago")
    ran: list[str] = []

    async def discover() -> None:
        ran.append("discover")

    async def build() -> None:
        ran.append("build")

    morning = datetime(2026, 10, 22, 12, 8, tzinfo=tz)
    with patch("core.jobs.discovery.scheduled_discover", new=discover), \
            patch("core.jobs.queue.scheduled_build", new=build):
        with patch("core.heartbeats.get_heartbeat", new=AsyncMock(return_value=morning - timedelta(hours=31))):
            assert await briefs.catch_up_discovery(morning) is True
        with patch("core.heartbeats.get_heartbeat", new=AsyncMock(return_value=morning - timedelta(hours=2))):
            assert await briefs.catch_up_discovery(morning) is False, "today's run happened"
        with patch("core.heartbeats.get_heartbeat", new=AsyncMock(return_value=None)):
            assert await briefs.catch_up_discovery(morning.replace(hour=4)) is False, "before 05:15 the cron runs it"
    assert ran == ["discover", "build"]


@pytest.fixture(autouse=True)
def _no_owed_promises(monkeypatch):
    # These tests compare the delivered text exactly; the promises appended
    # from config/context.yaml are covered by tests/test_owed_promises.py.
    monkeypatch.setattr("core.loop.briefs._owes", lambda: "")
