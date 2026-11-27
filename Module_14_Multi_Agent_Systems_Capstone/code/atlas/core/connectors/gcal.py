"""Atlas -- Google Calendar connector.

Replaces the ICS feed for sensing: recurring events expand correctly,
changes and cancellations are seen, and a syncToken makes every tick
after the first a single cheap request. Each new or changed event in the
window (one day back, fourteen days ahead) becomes a signal with the
start date as its deadline; triage links it to a project. The COO's
upcoming_events tool keeps reading the ICS feed until R4 gives it this
API with write access for calendar holds.

Cursor (connector_state, source "gcal"): {"sync_token": "..."}. A 410
means the token expired; the connector does a full window sync again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from core import privacy
from core.config import settings
from core.connectors import state
from core.connectors.base import Connector, ConnectorError, TransientConnectorError
from core.connectors.google_auth import (
    GoogleNeedsConsent,
    TokenProvider,
    configured,
    default_token_provider,
    invalidate,
)
from core.signals import Signal, insert_signal

log = logging.getLogger(__name__)

SOURCE = "gcal"
API = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = httpx.Timeout(20.0)
_LOOKBACK = timedelta(days=1)
_LOOKAHEAD = timedelta(days=14)
_MAX_PAGES = 5

ClientFactory = Callable[[], httpx.AsyncClient]


class SyncTokenGone(ConnectorError):
    """410: the sync token expired; full sync again."""


def _when(block: dict[str, Any] | None) -> tuple[datetime | None, bool]:
    """(start datetime in UTC, all_day) from an event start/end block."""
    if not block:
        return None, False
    if block.get("dateTime"):
        try:
            return datetime.fromisoformat(block["dateTime"].replace("Z", "+00:00")).astimezone(UTC), False
        except ValueError:
            return None, False
    if block.get("date"):
        try:
            d = date.fromisoformat(block["date"])
        except ValueError:
            return None, True
        # An all-day event is a local date; anchor it at local midnight so
        # the timezone conversion below cannot slide it to the day before.
        return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(settings.timezone)), True
    return None, False


def event_to_signal(event: dict[str, Any]) -> Signal | None:
    """Map one events.list item to a Signal; None for empty placeholders."""
    summary = (event.get("summary") or "").strip()
    status = event.get("status") or "confirmed"
    if not summary and status == "cancelled":
        return None  # a cancelled instance we never saw has nothing to say
    start, all_day = _when(event.get("start"))
    tz = ZoneInfo(settings.timezone)
    if start is not None:
        local = start.astimezone(tz)
        when = local.strftime("%a %b %d") if all_day else local.strftime("%a %b %d %H:%M")
        deadline = local.date()
    else:
        when, deadline = "", None
    prefix = "Cancelled: " if status == "cancelled" else "Calendar: "
    subject = f"{prefix}{summary or '(untitled)'}" + (f" on {when}" if when else "")
    lines = []
    if event.get("location"):
        lines.append(f"Where: {event['location']}")
    attendees = [a.get("displayName") or a.get("email") for a in event.get("attendees") or []]
    attendees = [a for a in attendees if a]
    if attendees:
        lines.append("With: " + ", ".join(attendees[:8]))
    if event.get("description"):
        lines.append((event["description"] or "")[:1000])
    updated = event.get("updated") or ""
    try:
        occurred = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else datetime.now(UTC)
    except ValueError:
        occurred = datetime.now(UTC)
    organizer = (event.get("organizer") or {}).get("displayName") or (event.get("organizer") or {}).get("email")
    # The privacy gate: a client's meeting is a client's mail (core/privacy.py).
    hit = privacy.is_private_event(
        title=summary, organizer=(event.get("organizer") or {}).get("email") or "",
        attendees=[a.get("email") or "" for a in event.get("attendees") or []])
    return Signal(
        source=SOURCE,
        private=hit is not None,
        private_rule=hit.label if hit else None,
        external_id=f"{event.get('id')}:{updated or 'na'}",
        occurred_at=occurred,
        actor=organizer,
        subject=subject[:300],
        body="\n".join(lines),
        url=event.get("htmlLink"),
        deadline=deadline if status != "cancelled" else None,
        raw={
            "type": "event",
            "event_id": event.get("id"),
            "status": status,
            "start": (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date"),
            "end": (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date"),
            "all_day": all_day,
        },
    )


class GoogleCalendarConnector(Connector):
    name = SOURCE
    cache_ttl_seconds = 0.0
    max_attempts = 3

    def __init__(
        self,
        calendar_id: str | None = None,
        token_provider: TokenProvider | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self.calendar_id = calendar_id or settings.google_calendar_id or "primary"
        self._token = token_provider or default_token_provider
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(base_url=API, timeout=_TIMEOUT))
        self._current_token: str | None = None

    async def _bearer(self) -> str:
        # Fetched once per sync outside the retry wrapper, so a consent
        # failure keeps its type. A 401 clears it and the retry re-fetches.
        if self._current_token is None:
            self._current_token = await self._token()
        return self._current_token

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        token = await self._bearer()
        path = f"/calendars/{httpx.URL(self.calendar_id).path}/events"
        async with self._client_factory() as client:
            try:
                resp = await client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise TransientConnectorError(f"gcal: {exc}") from exc
        if resp.status_code == 401:
            invalidate()
            self._current_token = None
            raise TransientConnectorError("gcal: token rejected; refreshed for retry")
        if resp.status_code == 410:
            raise SyncTokenGone("gcal sync token expired")
        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientConnectorError(f"gcal: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise ConnectorError(f"gcal: HTTP {resp.status_code} {resp.text[:120]}")
        return resp.json()

    async def _fetch_fresh(self, key: str) -> Any:
        """key is 'full' or 'token:<syncToken>'. Returns {items, syncToken}."""
        kind, _, token = key.partition(":")
        items: list[dict[str, Any]] = []
        page: str | None = None
        next_sync: str | None = None
        for _ in range(_MAX_PAGES):
            if kind == "full":
                now = datetime.now(UTC)
                params: dict[str, Any] = {
                    "timeMin": (now - _LOOKBACK).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "timeMax": (now + _LOOKAHEAD).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "singleEvents": "true",
                    "maxResults": 250,
                }
            else:
                params = {"syncToken": token, "maxResults": 250}
            if page:
                params["pageToken"] = page
            data = await self._get(params)
            items.extend(data.get("items") or [])
            page = data.get("nextPageToken")
            next_sync = data.get("nextSyncToken") or next_sync
            if not page:
                break
        return {"items": items, "syncToken": next_sync}

    # -- hands: holds under approval, and the COO's read ------------------------

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        *,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event (a hold). Gated by the approval policy
        through the tool name that calls it. Returns {event_id, url, start, end}."""
        if end <= start:
            raise ValueError("end must be after start")
        tz = settings.timezone
        body: dict[str, Any] = {
            "summary": summary[:200],
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
        if description:
            body["description"] = description[:2000]
        if location:
            body["location"] = location[:200]
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees[:20]]
        token = await self._bearer()
        path = f"/calendars/{httpx.URL(self.calendar_id).path}/events"
        async with self._client_factory() as client:
            try:
                resp = await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise TransientConnectorError(f"gcal create: {exc}") from exc
        if resp.status_code == 403:
            raise ConnectorError(
                "Google is connected read-only (docs/privacy.md), so Atlas cannot create calendar events. "
                "Add the hold in Google Calendar yourself.")
        if resp.status_code >= 400:
            raise ConnectorError(f"gcal create: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return {
            "event_id": data.get("id"),
            "url": data.get("htmlLink"),
            "start": (data.get("start") or {}).get("dateTime"),
            "end": (data.get("end") or {}).get("dateTime"),
        }

    async def upcoming(self, days: int = 7, *, limit: int = 50) -> list[dict[str, Any]]:
        """Events from now through `days` ahead, recurring ones expanded,
        as plain dicts for the COO: {summary, start, end, all_day, location, url}."""
        now = datetime.now(UTC)
        data = await self._get(
            {
                "timeMin": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "timeMax": (now + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max(1, min(limit, 250)),
            }
        )
        out = []
        tz = ZoneInfo(settings.timezone)
        for item in data.get("items") or []:
            if item.get("status") == "cancelled":
                continue
            start, all_day = _when(item.get("start"))
            end, _ = _when(item.get("end"))
            out.append(
                {
                    "summary": item.get("summary") or "(untitled)",
                    "start": start.astimezone(tz).isoformat() if start else None,
                    "end": end.astimezone(tz).isoformat() if end else None,
                    "all_day": all_day,
                    "location": item.get("location"),
                    "url": item.get("htmlLink"),
                }
            )
        return out

    async def sync(self) -> dict[str, Any]:
        """Pull new and changed events into signals. Never raises."""
        summary: dict[str, Any] = {"events": 0, "new_signals": 0, "failures": []}
        if not configured():
            log.info("gcal connector: google oauth not configured; skipping")
            return summary
        cursor = await state.get_cursor(SOURCE)
        sync_token = cursor.get("sync_token")
        self._current_token = None
        try:
            await self._bearer()
            try:
                key = f"token:{sync_token}" if sync_token else "full"
                data = (await self.get(key, allow_stale=False)).data
            except (SyncTokenGone, ConnectorError) as exc:
                if not isinstance(exc, SyncTokenGone) and "sync token expired" not in str(exc):
                    raise
                log.info("gcal connector: sync token expired; full sync")
                data = (await self.get("full", allow_stale=False)).data
        except GoogleNeedsConsent as exc:
            await state.record_failure(SOURCE, f"auth: {exc}")
            summary["failures"].append("auth")
            return summary
        except Exception as exc:
            log.warning("gcal connector: listing failed: %s", exc)
            await state.record_failure(SOURCE, f"list: {exc}")
            summary["failures"].append("list")
            return summary

        for item in data.get("items") or []:
            summary["events"] += 1
            sig = event_to_signal(item)
            if sig is None:
                continue
            if await insert_signal(sig) is not None:
                summary["new_signals"] += 1
        if data.get("syncToken"):
            await state.set_cursor(SOURCE, {"sync_token": data["syncToken"]})
        else:
            await state.record_ok(SOURCE)
        return summary


__all__ = ["GoogleCalendarConnector", "event_to_signal", "SOURCE"]
