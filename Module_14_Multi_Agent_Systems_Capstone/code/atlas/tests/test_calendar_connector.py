"""Unit tests for the Calendar ICS connector and the COO's
upcoming_events tool.

Network calls are mocked; ICS parsing is exercised against synthetic
documents covering TZID, UTC, floating, all-day, recurring, and
cancelled events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agents.coo.tools import build_tools as coo_build_tools
from core.connectors.calendar import CalendarEvent, parse_ics


def _coo_tool(name: str):
    return {t.name: t for t in coo_build_tools()}[name].handler


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:one
SUMMARY:Team standup
DTSTART;TZID=America/Chicago:20260602T090000
DTEND;TZID=America/Chicago:20260602T093000
LOCATION:Conference room A
END:VEVENT
BEGIN:VEVENT
UID:two
SUMMARY:Customer call (UTC)
DTSTART:20260603T140000Z
DTEND:20260603T150000Z
END:VEVENT
BEGIN:VEVENT
UID:three
SUMMARY:All-day birthday
DTSTART;VALUE=DATE:20260615
END:VEVENT
BEGIN:VEVENT
UID:four
SUMMARY:Weekly recurring meeting
DTSTART;TZID=America/Chicago:20260601T100000
RRULE:FREQ=WEEKLY;BYDAY=MO
END:VEVENT
BEGIN:VEVENT
UID:five
SUMMARY:Cancelled event
DTSTART:20260605T140000Z
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:six
SUMMARY:Long, escaped\\, summary; with chars
DTSTART:20260606T140000Z
END:VEVENT
END:VCALENDAR
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_ics_skips_recurring_and_cancelled():
    events = parse_ics(SAMPLE_ICS, default_tz="America/Chicago")
    summaries = [e.summary for e in events]
    assert "Team standup" in summaries
    assert "Customer call (UTC)" in summaries
    assert "All-day birthday" in summaries
    # Recurring + cancelled must be excluded.
    assert "Weekly recurring meeting" not in summaries
    assert "Cancelled event" not in summaries


def test_parse_ics_tzid_event_lands_in_named_timezone():
    events = parse_ics(SAMPLE_ICS, default_tz="UTC")
    standup = next(e for e in events if e.summary == "Team standup")
    assert standup.start.tzinfo is not None
    # CDT in June is UTC-5 -> 09:00 local == 14:00 UTC
    utc = standup.start.astimezone(UTC)
    assert utc.hour == 14


def test_parse_ics_utc_event_lands_in_utc():
    events = parse_ics(SAMPLE_ICS, default_tz="America/Chicago")
    call = next(e for e in events if e.summary == "Customer call (UTC)")
    assert call.start.tzinfo is not None
    assert call.start.astimezone(UTC).hour == 14


def test_parse_ics_all_day_event():
    events = parse_ics(SAMPLE_ICS, default_tz="America/Chicago")
    bday = next(e for e in events if e.summary == "All-day birthday")
    assert bday.all_day is True
    assert bday.end is None


def test_parse_ics_decodes_escaped_chars_in_summary():
    events = parse_ics(SAMPLE_ICS, default_tz="UTC")
    six = next(e for e in events if "Long" in e.summary)
    assert "summary;" in six.summary or "summary; with" in six.summary
    # The escaped comma should be decoded.
    assert "escaped," in six.summary


def test_parse_ics_empty_string_returns_empty_list():
    assert parse_ics("", default_tz="UTC") == []


def test_parse_ics_unfolds_continuation_lines():
    """RFC 5545 line folding: a continuation line starts with space."""
    folded = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "UID:fold\n"
        "SUMMARY:A long summary that\n"
        " wraps across two lines\n"
        "DTSTART:20260607T140000Z\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    events = parse_ics(folded, default_tz="UTC")
    assert len(events) == 1
    assert "wraps across two lines" in events[0].summary


# ---------------------------------------------------------------------------
# upcoming_events tool
# ---------------------------------------------------------------------------


async def test_upcoming_events_reports_not_configured_when_url_blank():
    with patch("agents.coo.tools._calendar_connector", return_value=None):
        out = await _coo_tool("upcoming_events")({"days_ahead": 14})
    assert out.get("configured") is False
    assert "not configured" in out["error"]


async def test_upcoming_events_filters_to_horizon():
    # Build synthetic events: one in 3 days, one in 60 days.
    now_local = datetime.now(ZoneInfo("America/Chicago"))
    near = CalendarEvent(
        uid="near",
        summary="Near meeting",
        start=now_local + timedelta(days=3),
        end=now_local + timedelta(days=3, hours=1),
        all_day=False,
        location=None,
    )
    far = CalendarEvent(
        uid="far",
        summary="Far meeting",
        start=now_local + timedelta(days=60),
        end=now_local + timedelta(days=60, hours=1),
        all_day=False,
        location=None,
    )
    fake_result = MagicMock()
    fake_result.data = [near, far]
    fake_result.fetched_at = 1234567890.0
    fake_result.stale = False

    fake_conn = MagicMock()
    fake_conn.get = AsyncMock(return_value=fake_result)

    with patch(
        "agents.coo.tools._calendar_connector", return_value=fake_conn
    ):
        out = await _coo_tool("upcoming_events")({"days_ahead": 14})

    assert out["configured"] is True
    summaries = [e["summary"] for e in out["events"]]
    assert "Near meeting" in summaries
    assert "Far meeting" not in summaries  # past 14-day horizon


async def test_upcoming_events_clamps_horizon():
    fake_result = MagicMock()
    fake_result.data = []
    fake_result.fetched_at = 0
    fake_result.stale = False
    fake_conn = MagicMock()
    fake_conn.get = AsyncMock(return_value=fake_result)
    with patch(
        "agents.coo.tools._calendar_connector", return_value=fake_conn
    ):
        out = await _coo_tool("upcoming_events")({"days_ahead": 9999})
    # Implementation clamps to 180.
    assert out["days_ahead"] == 180


async def test_coo_toolset_includes_upcoming_events():
    names = {t.name for t in coo_build_tools()}
    assert "upcoming_events" in names


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
