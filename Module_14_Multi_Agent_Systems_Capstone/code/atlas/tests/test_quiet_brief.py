"""Efficiency lever 3b (r12): nothing new since the last brief, no model call.

Every table the change check and the template read is a private copy; the
CEO dispatch is a mock that fails the test if it is reached when it should
not be.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from core import db
from core.config import settings
from core.loop import briefs
from tests import livedb

TABLES = ("system_heartbeats", "signals", "approvals", "deadlines", "night_tasks", "goals",
          "job_events", "recordings", "briefs", "notifications", "triggers", "projects")
NOW = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)  # 20:00 in Houston


@pytest_asyncio.fixture(autouse=True)
async def _private(monkeypatch):
    monkeypatch.setattr(settings, "brief_skip_unchanged", True)
    # Nothing connected unless a test says otherwise (the laptop today).
    monkeypatch.setattr("core.connect.status", AsyncMock(return_value=_tiles()))
    async with livedb.shadow(monkeypatch, *TABLES):
        yield


def _tiles(google: datetime | None | bool = False, canvas: datetime | None | bool = False,
           github: datetime | None | bool = False) -> dict:
    """connect.status(check=False) as the quiet brief reads it. False: not
    connected; None: connected, never synced; a datetime: last good sync."""
    def state(v):
        return {"last_ok_at": v} if isinstance(v, datetime) else None
    return {
        "google": {"consented": google is not False, "needs_reconnect": False,
                   "gmail": state(google), "calendar": state(google)},
        "canvas": {"configured": canvas is not False, "state": state(canvas)},
        "github": {"configured": github is not False, "state": state(github)},
    }


async def _quiet_body() -> str:
    await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=12))
    sent = AsyncMock(return_value=None)
    with patch("core.loop.briefs.notify", new=sent), patch("core.loop.briefs.dispatch", new=_ceo()):
        assert await briefs._try_quiet("evening", NOW) is True
    return sent.await_args.kwargs["body"]


async def _last(at: datetime, full_at: datetime) -> None:
    await db.execute(
        "insert into system_heartbeats (kind, last_at, metadata) values ($1, now(), $2)",
        briefs.LAST_BRIEF, {"at": at.isoformat(), "full_at": full_at.isoformat(), "full": True},
    )


def _ceo() -> AsyncMock:
    return AsyncMock(side_effect=AssertionError("the model was called"))


async def test_nothing_changed_sends_the_template_and_no_model_call():
    await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=12))
    sent = AsyncMock(return_value=None)
    with patch("core.loop.briefs.notify", new=sent), patch("core.loop.briefs.dispatch", new=_ceo()):
        assert await briefs._try_quiet("evening", NOW) is True
    body = sent.await_args.kwargs["body"]
    assert body.startswith("Evening brief, Wed Oct 21. Nothing new since the last brief at 08:00")
    assert "Written without the model" in body and "Today's spend" in body
    assert "—" not in body
    assert await db.fetchval("select count(*) from briefs where kind = 'evening'") == 1
    last = await briefs._last_brief()
    assert last is not None and last["at"] == NOW and last["full_at"] == NOW - timedelta(hours=12)


@pytest.mark.parametrize("insert", [
    "insert into signals (source, external_id, occurred_at, subject) values ('gmail', 'x1', now(), 'hi')",
    "insert into deadlines (title, source) values ('Email the lender', 'owner')",
    "insert into goals (description) values ('Ship the resume engine')",
])
async def test_anything_new_means_a_full_brief(insert):
    await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=12))
    await db.execute(insert.replace("now()", "'2026-10-21T20:00:00Z'") if "signals" in insert else insert)
    with patch("core.loop.briefs.notify", new=AsyncMock()) as sent:
        assert await briefs._try_quiet("evening", datetime.now(UTC) + timedelta(seconds=1)) is False
    sent.assert_not_awaited()


async def test_a_full_brief_goes_out_at_least_every_two_days():
    await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=49))
    with patch("core.loop.briefs.notify", new=AsyncMock()) as sent:
        assert await briefs._try_quiet("evening", NOW) is False
    sent.assert_not_awaited()


async def test_off_when_the_flag_is_off_and_never_without_a_full_brief_on_record(monkeypatch):
    with patch("core.loop.briefs.notify", new=AsyncMock()) as sent:
        assert await briefs._try_quiet("morning", NOW) is False  # no brief on record yet
        await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=12))
        monkeypatch.setattr(settings, "brief_skip_unchanged", False)
        assert await briefs._try_quiet("morning", NOW) is False
    sent.assert_not_awaited()


async def test_a_full_brief_records_itself_for_the_next_check():
    with (
        patch("core.loop.briefs._signals_context", new=AsyncMock(return_value="nothing")),
        patch("core.loop.briefs._dispatch_with_retry",
              new=AsyncMock(return_value=type("R", (), {"text": "Full brief."})())),
        patch("core.loop.briefs.notify", new=AsyncMock(return_value=None)),
        patch("core.loop.briefs.daily_health_line", new=AsyncMock(return_value="health")),
        patch("core.loop.briefs.daily_cost_line", new=AsyncMock(return_value="cost")),
    ):
        await briefs.run_brief("morning")
    last = await briefs._last_brief()
    assert last is not None and last["at"] == last["full_at"]


async def test_disconnected_sources_are_named_and_never_called_quiet():
    body = await _quiet_body()
    assert ("Not checked: Gmail, Calendar, Canvas and GitHub are not connected, so mail, the calendar, "
            "assignments and repos were not looked at.") in body
    assert "no new signals" not in body


async def test_only_fresh_sources_count_as_checked(monkeypatch):
    fresh = NOW - timedelta(hours=1)
    monkeypatch.setattr("core.connect.status",
                        AsyncMock(return_value=_tiles(google=fresh, canvas=NOW - timedelta(hours=9), github=None)))
    body = await _quiet_body()
    assert "no new signals from Gmail and Calendar," in body
    assert "Not checked: Canvas is not synced in the last 6 hours, so assignments were not looked at." in body
    assert "Not checked: GitHub is connected but never synced, so repos were not looked at." in body


async def test_everything_fresh_means_no_not_checked_line(monkeypatch):
    fresh = NOW - timedelta(minutes=30)
    monkeypatch.setattr("core.connect.status",
                        AsyncMock(return_value=_tiles(google=fresh, canvas=fresh, github=fresh)))
    body = await _quiet_body()
    assert "no new signals from Gmail, Calendar, Canvas and GitHub," in body and "Not checked" not in body


async def test_when_coverage_cannot_be_read_a_full_brief_goes_out(monkeypatch):
    await _last(NOW - timedelta(hours=12), NOW - timedelta(hours=12))
    monkeypatch.setattr("core.connect.status", AsyncMock(side_effect=OSError("env unreadable")))
    with patch("core.loop.briefs.notify", new=AsyncMock(return_value=None)) as sent:
        assert await briefs._try_quiet("evening", NOW) is False
    sent.assert_not_awaited()


async def test_a_quiet_brief_carries_the_promises_owed(monkeypatch):
    from core import context_pack
    from tests.test_owed_promises import BRENDA, CARTAS, PACK

    monkeypatch.setattr(context_pack, "load", lambda path=None: PACK)
    body = await _quiet_body()
    assert BRENDA in body and CARTAS in body
    assert body.index("Not checked") < body.index("Promises you owe")


async def test_the_week_audit_reads_quiet_briefs_only():
    from datetime import date

    from scripts.quiet_brief_audit import audit

    await db.execute("insert into briefs (kind, body, created_at) values ('morning', 'A full brief.', '2026-10-23T13:02:00Z')")
    await db.execute("insert into briefs (kind, body, created_at) values ('evening', $1, '2026-10-24T01:01:00Z')",
                     "Evening brief.\n\nNot checked: Gmail is not connected.\n\nPromises you owe (1):\n- Brenda\n\n"
                     "Written without the model because nothing Atlas could check had changed.")
    await db.execute("insert into briefs (kind, body, created_at) values ('morning', $1, '2026-10-24T13:01:00Z')",
                     "Morning brief.\n\nWritten without the model because nothing Atlas could check had changed.")
    out = await audit(date(2026, 10, 22))
    assert out["briefs"] == 3 and out["quiet"] == 2
    assert out["all_not_checked"] is False and out["all_owes"] is False
    assert [i["owes"] for i in out["items"]] == [True, False]
