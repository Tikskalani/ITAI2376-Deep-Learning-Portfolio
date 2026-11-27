"""Tests for the Google auth helper and the Gmail and Calendar connectors.
httpx goes through MockTransport; the database is the live test one."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from core import signals
from core.connectors import gcal, gmail, google_auth, state
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    monkeypatch.setattr(google_auth.settings, "google_client_id", "cid")
    monkeypatch.setattr(google_auth.settings, "google_client_secret", "sec")
    monkeypatch.setattr(google_auth.settings, "google_refresh_token", "rt-test")
    google_auth.invalidate()
    # Real mail and calendar signals, their cursors and health alerts stay untouched.
    async with livedb.shadow(monkeypatch, "signals", "connector_state", "notifications"):
        yield
    google_auth.invalidate()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


async def test_access_token_refreshes_once_then_caches_and_maps_errors():
    calls: list[httpx.Request] = []
    answer = {"status": 200, "json": {"access_token": "at-1", "expires_in": 3600}}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(answer["status"], json=answer["json"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await google_auth.access_token(client) == "at-1"
        assert await google_auth.access_token(client) == "at-1"
        assert len(calls) == 1
        body = dict(x.split("=") for x in calls[0].content.decode().split("&"))
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "rt-test"

        google_auth.invalidate()
        answer.update(status=400, json={"error": "invalid_grant"})
        with pytest.raises(google_auth.GoogleNeedsConsent):
            await google_auth.access_token(client)
        assert len(calls) == 2 and google_auth.needs_reconnect()
        with pytest.raises(google_auth.GoogleNeedsConsent):
            await google_auth.access_token(client)                  # remembered; Google is not asked again
        assert len(calls) == 2
        google_auth.invalidate()                                    # a new token (the Connect tab) lifts the block
        assert not google_auth.needs_reconnect()
        answer.update(status=503, json={})
        with pytest.raises(google_auth.TransientConnectorError):
            await google_auth.access_token(client)
        answer.update(status=400, json={"error": "invalid_client"})
        with pytest.raises(google_auth.GoogleAuthError):
            await google_auth.access_token(client)


async def test_not_configured(monkeypatch):
    monkeypatch.setattr(google_auth.settings, "google_refresh_token", "")
    assert not google_auth.configured()
    with pytest.raises(google_auth.GoogleAuthError):
        await google_auth.access_token()
    assert (await gmail.GmailConnector().sync())["messages"] == 0
    assert (await gcal.GoogleCalendarConnector().sync())["events"] == 0


# ---------------------------------------------------------------------------
# gmail mapping
# ---------------------------------------------------------------------------


def _msg(mid: str, *, labels=("INBOX",), text="Hi Trilok, can you send the invoice?", html=None, snippet="snip"):
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "snippet": snippet,
        "internalDate": str(int(datetime(2026, 10, 7, 14, 0, tzinfo=UTC).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "Chris Lee <person6@demodental.example>"},
                        {"name": "To", "value": "person3@gmail.com"},
                        {"name": "Subject", "value": "Invoice for October"}],
            "parts": parts,
        },
    }


async def test_message_to_signal_and_skips():
    s = gmail.message_to_signal(_msg("m1"))
    assert s is not None
    assert s.actor == "Chris Lee" and s.subject == "Invoice for October"
    assert s.body == "Hi Trilok, can you send the invoice?"
    assert s.url == "https://mail.google.com/mail/u/0/#all/t-m1"
    assert s.occurred_at == datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    assert s.raw["from"] == "person6@demodental.example" and s.external_id == "m1"

    html_only = gmail.message_to_signal(_msg("m2", text=None, html="<p>Hello <b>there</b></p>"))
    assert html_only is not None and html_only.body == "Hello there"
    snippet_only = gmail.message_to_signal(_msg("m3", text=None))
    assert snippet_only is not None and snippet_only.body == "snip"
    assert gmail.message_to_signal(_msg("m4", labels=("CATEGORY_PROMOTIONS",))) is None
    assert gmail.message_to_signal(_msg("m5", labels=("SENT",))) is None


def _gmail_transport(calls: list[httpx.Request], *, history_status=200, history_ids=("h1",), recent_ids=("r1", "r2"), profile_history="900"):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        p = req.url.path
        if p.endswith("/profile"):
            return httpx.Response(200, json={"historyId": profile_history})
        if p.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": i} for i in recent_ids]})
        if p.endswith("/history"):
            if history_status != 200:
                return httpx.Response(history_status)
            return httpx.Response(200, json={
                "historyId": "950",
                "history": [{"messagesAdded": [{"message": {"id": i}} for i in history_ids]}],
            })
        mid = p.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_msg(mid))
    return httpx.MockTransport(handler)


def _gmail(transport) -> gmail.GmailConnector:
    async def token():
        return "at"
    return gmail.GmailConnector(token_provider=token, client_factory=lambda: httpx.AsyncClient(
        base_url=gmail.API, transport=transport))


async def test_gmail_first_sync_lists_recent_then_walks_history_then_recovers_from_old_history():
    calls: list[httpx.Request] = []
    c = _gmail(_gmail_transport(calls))
    out = await c.sync()
    assert {k: out[k] for k in ("messages", "new_signals", "failures")} == {"messages": 2, "new_signals": 2, "failures": []}
    assert out["waiting_for"] == 0  # the mock has no sent threads
    assert calls[0].headers["Authorization"] == "Bearer at"
    assert any("newer_than" in str(r.url) for r in calls)
    assert (await state.get_cursor("gmail"))["history_id"] == "900"
    mine = [r for r in await signals.list_signals("new", limit=100) if r["source"] == "gmail"]
    assert {r["external_id"] for r in mine} == {"r1", "r2"}

    calls.clear()
    c = _gmail(_gmail_transport(calls, history_ids=("r2", "h1")))
    out = await c.sync()
    assert out["new_signals"] == 1 and out["messages"] == 2  # r2 already known
    assert any("startHistoryId=900" in str(r.url) for r in calls)
    assert (await state.get_cursor("gmail"))["history_id"] == "950"

    calls.clear()
    c = _gmail(_gmail_transport(calls, history_status=404, recent_ids=("r9",)))
    c.max_attempts = 1
    out = await c.sync()
    assert out["new_signals"] == 1 and out["failures"] == []
    assert any(r.url.path.endswith("/messages") and "newer_than" in str(r.url) for r in calls)


async def test_gmail_consent_expiry_records_and_notifies_once(monkeypatch):
    async def expired():
        raise google_auth.GoogleNeedsConsent("expired")

    sent: list[dict] = []

    async def fake_notify(**kw):
        sent.append(kw)
        return None

    monkeypatch.setattr("core.surfaces.notify.notify", fake_notify)
    c = gmail.GmailConnector(token_provider=expired, client_factory=lambda: httpx.AsyncClient())
    assert (await c.sync())["failures"] == ["auth"]
    assert (await c.sync())["failures"] == ["auth"]
    assert len(sent) == 1 and sent[0]["kind"] == "health" and sent[0]["priority"] == "high"
    s = await state.get_state("gmail")
    assert s["consecutive_failures"] == 2 and s["last_error"].startswith("auth:")


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


def _event(eid: str, *, status="confirmed", start=None, all_day=False, updated="2026-10-07T12:00:00Z"):
    if all_day:
        start_block, end_block = {"date": "2026-10-09"}, {"date": "2026-10-10"}
    else:
        start_block = {"dateTime": start or "2026-10-09T15:00:00-05:00"}
        end_block = {"dateTime": "2026-10-09T16:00:00-05:00"}
    return {
        "id": eid, "status": status, "summary": "Demo Dental sync", "updated": updated,
        "start": start_block, "end": end_block, "htmlLink": f"https://cal/{eid}",
        "location": "Zoom", "attendees": [{"displayName": "Chris"}, {"email": "x@y.z"}],
        "organizer": {"email": "person3@gmail.com"}, "description": "Review the portal",
    }


async def test_event_to_signal_shapes(monkeypatch):
    monkeypatch.setattr(gcal.settings, "timezone", "America/Chicago")
    s = gcal.event_to_signal(_event("e1"))
    assert s is not None
    assert s.subject == "Calendar: Demo Dental sync on Fri Oct 09 15:00"
    assert s.deadline == datetime(2026, 10, 9).date()
    assert "Where: Zoom" in s.body and "With: Chris, x@y.z" in s.body and "Review the portal" in s.body
    assert s.external_id == "e1:2026-10-07T12:00:00Z" and s.url == "https://cal/e1"
    allday = gcal.event_to_signal(_event("e2", all_day=True))
    assert allday is not None and allday.subject == "Calendar: Demo Dental sync on Fri Oct 09"
    cancelled = gcal.event_to_signal(_event("e3", status="cancelled"))
    assert cancelled is not None and cancelled.subject.startswith("Cancelled: ") and cancelled.deadline is None
    assert gcal.event_to_signal({"id": "ghost", "status": "cancelled"}) is None


def _gcal_transport(calls: list[httpx.Request], *, token_status=200, items=None, next_sync="sync-2"):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if "syncToken" in req.url.params and token_status != 200:
            return httpx.Response(token_status)
        return httpx.Response(200, json={"items": items or [], "nextSyncToken": next_sync})
    return httpx.MockTransport(handler)


def _gcal(transport) -> gcal.GoogleCalendarConnector:
    async def token():
        return "at"
    return gcal.GoogleCalendarConnector(calendar_id="primary", token_provider=token,
                                        client_factory=lambda: httpx.AsyncClient(base_url=gcal.API, transport=transport))


async def test_gcal_full_sync_then_token_sync_then_410_recovery():
    calls: list[httpx.Request] = []
    out = await _gcal(_gcal_transport(calls, items=[_event("e1"), _event("e2", all_day=True)])).sync()
    assert out == {"events": 2, "new_signals": 2, "failures": []}
    assert calls[0].url.path.endswith("/calendars/primary/events")
    assert "timeMin" in calls[0].url.params and calls[0].url.params["singleEvents"] == "true"
    assert (await state.get_cursor("gcal")) == {"sync_token": "sync-2"}

    calls.clear()
    out = await _gcal(_gcal_transport(calls, items=[_event("e1", updated="2026-10-08T09:00:00Z")], next_sync="sync-3")).sync()
    assert out["new_signals"] == 1  # the moved event surfaces again
    assert calls[0].url.params["syncToken"] == "sync-2"
    assert (await state.get_cursor("gcal")) == {"sync_token": "sync-3"}

    calls.clear()
    c = _gcal(_gcal_transport(calls, token_status=410, items=[_event("e5")], next_sync="sync-4"))
    c.max_attempts = 1
    out = await c.sync()
    assert out["new_signals"] == 1 and out["failures"] == []
    assert "syncToken" in calls[0].url.params and "timeMin" in calls[-1].url.params
    assert (await state.get_cursor("gcal")) == {"sync_token": "sync-4"}


async def test_senses_roster_includes_google_connectors():
    from core.loop.senses import connectors

    names = [c.name for c in connectors()]
    assert names == ["github", "canvas", "gmail", "gcal"]
    assert json.dumps(names)  # trivially serialisable for the heartbeat
    assert timedelta(days=14) == gcal._LOOKAHEAD
