"""Tests for the agents' Google hands: client_delivery.draft_email and the
COO's calendar_create_event and API-backed upcoming_events. Connectors
are replaced; nothing touches the network or the database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from agents.client_delivery.tools import build_tools as cd_tools
from agents.coo.tools import build_tools as coo_tools
from core.runtime.policy import match_gate


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


async def test_draft_email_requires_fields_and_config_then_delegates():
    tool = _tool(cd_tools(), "draft_email")
    assert "never sends" in tool.description
    assert (await tool.handler({"to": "a@b.c", "subject": "s"}))["error"].startswith("to, subject and body")

    with patch("core.connectors.google_auth.configured", return_value=False):
        out = await tool.handler({"to": "a@b.c", "subject": "s", "body": "b"})
    assert out["configured"] is False and "not connected" in out["error"]

    conn = MagicMock()
    conn.create_draft = AsyncMock(return_value={"draft_id": "d1", "message_id": "m1", "thread_id": "t1", "url": "u"})
    with (
        patch("core.connectors.google_auth.configured", return_value=True),
        patch("core.connectors.gmail.GmailConnector", return_value=conn),
    ):
        out = await tool.handler({"to": "a@b.c", "subject": "Re: x", "body": "hi", "cc": " ",
                                  "thread_id": "t1", "in_reply_to": "<m@x>"})
    assert out == {"configured": True, "sent": False, "draft_id": "d1", "message_id": "m1",
                   "thread_id": "t1", "url": "u"}
    conn.create_draft.assert_awaited_once_with("a@b.c", "Re: x", "hi", cc=None, thread_id="t1", in_reply_to="<m@x>")

    conn.create_draft = AsyncMock(side_effect=RuntimeError("quota"))
    with (
        patch("core.connectors.google_auth.configured", return_value=True),
        patch("core.connectors.gmail.GmailConnector", return_value=conn),
    ):
        out = await tool.handler({"to": "a@b.c", "subject": "s", "body": "b"})
    assert "quota" in out["error"]


async def test_calendar_create_event_parses_times_and_is_gated_by_policy():
    tool = _tool(coo_tools(), "calendar_create_event")
    # The seeded policy row '*create_event*' makes this an approval card.
    assert match_gate(tool.name, [{"tool_pattern": "*create_event*", "gate": "approve"}])[0] == "approve"

    assert "required" in (await tool.handler({"title": "x"}))["error"]
    assert "required" in (await tool.handler({"title": "x", "start": "not a date"}))["error"]
    assert "after start" in (await tool.handler({"title": "x", "start": "2026-10-09T10:00", "end": "2026-10-09T09:00"}))["error"]

    with patch("core.connectors.google_auth.configured", return_value=False):
        out = await tool.handler({"title": "Hold", "start": "2026-10-09T10:00"})
    assert out["configured"] is False

    conn = MagicMock()
    conn.create_event = AsyncMock(return_value={"event_id": "e1", "url": "u", "start": "s", "end": "e"})
    with (
        patch("core.connectors.google_auth.configured", return_value=True),
        patch("core.connectors.gcal.GoogleCalendarConnector", return_value=conn),
        patch("agents.coo.tools.settings.timezone", "America/Chicago"),
    ):
        out = await tool.handler({"title": "Hold", "start": "2026-10-09T10:00", "duration_minutes": 30,
                                  "attendees": "a@b.c, d@e.f", "location": "Zoom"})
    assert out["configured"] is True and out["event_id"] == "e1"
    args, kwargs = conn.create_event.await_args
    assert args[0] == "Hold"
    assert args[1].isoformat() == "2026-10-09T10:00:00-05:00"  # naive input read in his timezone
    assert args[2] - args[1] == timedelta(minutes=30)
    assert kwargs["attendees"] == ["a@b.c", "d@e.f"] and kwargs["location"] == "Zoom"

    with (
        patch("core.connectors.google_auth.configured", return_value=True),
        patch("core.connectors.gcal.GoogleCalendarConnector", return_value=conn),
    ):
        out = await tool.handler({"title": "Hold", "start": "2026-10-09T15:00:00Z", "end": "2026-10-09T16:00:00Z"})
    assert conn.create_event.await_args.args[1] == datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


async def test_upcoming_events_prefers_the_api_when_google_is_connected():
    tool = _tool(coo_tools(), "upcoming_events")
    conn = MagicMock()
    conn.upcoming = AsyncMock(return_value=[{"summary": "Standup", "start": "s", "end": "e", "all_day": False}])
    with (
        patch("core.connectors.google_auth.configured", return_value=True),
        patch("core.connectors.gcal.GoogleCalendarConnector", return_value=conn),
    ):
        out = await tool.handler({"days_ahead": 3})
    assert out["source"] == "api" and out["count"] == 1 and out["days_ahead"] == 3
    conn.upcoming.assert_awaited_once_with(days=3)

    with (
        patch("core.connectors.google_auth.configured", return_value=False),
        patch("agents.coo.tools.settings.calendar_ics_url", None),
    ):
        out = await tool.handler({})
    assert out["configured"] is False and "scripts.google_auth" in out["error"]
