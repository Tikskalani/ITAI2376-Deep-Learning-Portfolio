"""Atlas -- the Google Calendar ICS connector.

Google Calendar exposes a private "secret address in iCal format" URL
per calendar. Atlas reads it over plain HTTP, no OAuth, no token
refresh, no Google API client. The URL lives in
ATLAS_CALENDAR_ICS_URL; it is never embedded in source.

Recurrence handling: deliberately limited. Recurring events (anything
with an RRULE) are skipped, per the architectural decision recorded
when this connector was scoped. The connector parses one-off events
only. Full RRULE expansion is a future follow-up; until then the
agent's reply should say so when relevant.

Three DTSTART shapes are handled, covering the common Google Calendar
exports:

  * DTSTART;TZID=America/Chicago:20260601T140000 (named timezone)
  * DTSTART:20260601T140000Z (UTC)
  * DTSTART;VALUE=DATE:20260601 (all-day)

A DTSTART without TZID and without trailing Z is a "floating" time
per RFC 5545; we interpret it in the configured local timezone.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict

from core.connectors.base import Connector, TransientConnectorError

_FETCH_TIMEOUT_S = 15.0


class CalendarEvent(BaseModel):
    """One non-recurring calendar event."""

    model_config = ConfigDict(frozen=True)

    uid: str
    summary: str
    start: datetime
    end: datetime | None
    all_day: bool
    location: str | None


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a continuation line starts with space or tab."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(text: str) -> str:
    """Decode the small set of RFC 5545 text escapes that show up in
    SUMMARY and LOCATION values."""
    return (
        text.replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\\\", "\\")
    )


_DT_TZID = re.compile(
    r"^[A-Z]+(?:;[A-Z]+=[^:;]+)*;TZID=([^:;]+)(?:;[^:]+)*:(\d{8}T\d{6})$"
)
_DT_UTC = re.compile(r"^[A-Z]+:(\d{8}T\d{6})Z$")
_DT_FLOAT = re.compile(r"^[A-Z]+:(\d{8}T\d{6})$")
_DT_DATE = re.compile(
    r"^[A-Z]+(?:;[A-Z]+=[^:;]+)*;VALUE=DATE:(\d{8})$"
)
_DT_DATE_BARE = re.compile(r"^[A-Z]+:(\d{8})$")


def _parse_dt(line: str, default_tz: str) -> tuple[datetime | None, bool]:
    """Parse one DTSTART/DTEND/DTSTAMP line. Returns (dt, is_all_day).

    On a value the parser does not recognize, returns (None, False);
    the caller treats that event as unparseable and skips.
    """
    m = _DT_TZID.match(line)
    if m:
        tz, ts = m.group(1), m.group(2)
        try:
            return (
                datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(
                    tzinfo=ZoneInfo(tz)
                ),
                False,
            )
        except Exception:
            # Unknown TZID: fall through to default tz.
            try:
                return (
                    datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(
                        tzinfo=ZoneInfo(default_tz)
                    ),
                    False,
                )
            except Exception:
                return None, False
    m = _DT_UTC.match(line)
    if m:
        try:
            return (
                datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
                    tzinfo=ZoneInfo("UTC")
                ),
                False,
            )
        except Exception:
            return None, False
    m = _DT_DATE.match(line) or _DT_DATE_BARE.match(line)
    if m and len(m.group(1)) == 8:
        try:
            return (
                datetime.strptime(m.group(1), "%Y%m%d").replace(
                    tzinfo=ZoneInfo(default_tz)
                ),
                True,
            )
        except Exception:
            return None, False
    m = _DT_FLOAT.match(line)
    if m:
        try:
            return (
                datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
                    tzinfo=ZoneInfo(default_tz)
                ),
                False,
            )
        except Exception:
            return None, False
    return None, False


def parse_ics(text: str, default_tz: str = "UTC") -> list[CalendarEvent]:
    """Parse an ICS document and return its one-off events.

    VEVENT blocks containing an RRULE are skipped (recurring events
    are deferred per the saved design memory). VEVENT blocks with
    STATUS:CANCELLED are also skipped.
    """
    events: list[CalendarEvent] = []
    in_event = False
    current: dict[str, Any] = {}
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            in_event = True
            current = {}
            continue
        if stripped == "END:VEVENT":
            in_event = False
            if current.get("has_rrule"):
                continue
            if current.get("cancelled"):
                continue
            start = current.get("start")
            if start is None:
                continue
            events.append(
                CalendarEvent(
                    uid=current.get("uid", ""),
                    summary=_unescape(current.get("summary", "(no title)")),
                    start=start,
                    end=current.get("end"),
                    all_day=current.get("all_day", False),
                    location=(
                        _unescape(current["location"])
                        if current.get("location")
                        else None
                    ),
                )
            )
            continue
        if not in_event:
            continue
        if line.startswith("UID:"):
            current["uid"] = line[4:]
        elif line.startswith("SUMMARY"):
            idx = line.find(":")
            current["summary"] = line[idx + 1 :] if idx > 0 else ""
        elif line.startswith("LOCATION"):
            idx = line.find(":")
            current["location"] = line[idx + 1 :] if idx > 0 else ""
        elif line.startswith("DTSTART"):
            dt, all_day = _parse_dt(line, default_tz)
            current["start"] = dt
            current["all_day"] = all_day
        elif line.startswith("DTEND"):
            dt, _ = _parse_dt(line, default_tz)
            current["end"] = dt
        elif line.startswith("RRULE"):
            current["has_rrule"] = True
        elif line.startswith("STATUS:CANCELLED"):
            current["cancelled"] = True
    return events


class CalendarConnector(Connector):
    """Reads a Google Calendar private ICS URL and returns its one-off events.

    The `get` key is ignored (the ICS URL is fixed at construction
    time); pass any string, e.g. ``"all"``. The result's ``data`` is a
    list[CalendarEvent], unfiltered.
    """

    name = "calendar_ics"
    # ICS feeds change infrequently; 10 minutes is a reasonable balance
    # between freshness and Google's rate limits.
    cache_ttl_seconds = 600.0

    def __init__(self, ics_url: str, default_tz: str = "UTC") -> None:
        super().__init__()
        self._url = ics_url
        self._default_tz = default_tz

    async def _fetch_fresh(self, key: str) -> list[CalendarEvent]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT_S),
                follow_redirects=True,
                headers={"User-Agent": "Atlas/1.0"},
            ) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPError as exc:
            raise TransientConnectorError(
                f"calendar ICS fetch: {type(exc).__name__}: {exc}"
            ) from exc
        return parse_ics(text, default_tz=self._default_tz)


__all__ = [
    "CalendarEvent",
    "CalendarConnector",
    "parse_ics",
]
