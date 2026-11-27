"""Tests for the write side of the Google connectors (drafts, calendar
holds, the COO's API read) and the waiting-for inference. httpx goes
through MockTransport; the database is the live test one."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from core import signals
from core.connectors import gcal, gmail, state
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Real mail and calendar signals and their cursors stay untouched.
    async with livedb.shadow(monkeypatch, "signals", "connector_state"):
        yield


async def _token() -> str:
    return "at"


def _gmail(transport) -> gmail.GmailConnector:
    return gmail.GmailConnector(token_provider=_token, client_factory=lambda: httpx.AsyncClient(
        base_url=gmail.API, transport=transport))


def _gcal(transport) -> gcal.GoogleCalendarConnector:
    return gcal.GoogleCalendarConnector(calendar_id="primary", token_provider=_token,
                                        client_factory=lambda: httpx.AsyncClient(base_url=gcal.API, transport=transport))


async def test_create_draft_posts_rfc822_raw_and_never_sends():
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"id": "d1", "message": {"id": "m1", "threadId": "t1"}})

    out = await _gmail(httpx.MockTransport(handler)).create_draft(
        "person6@demodental.example", "Re: Invoice for October", "Hi Chris,\n\nAttached.\n\nTrilok",
        cc="x@y.z", thread_id="t1", in_reply_to="<abc@mail>",
    )
    assert out == {"draft_id": "d1", "message_id": "m1", "thread_id": "t1",
                   "url": "https://mail.google.com/mail/u/0/#drafts"}
    assert len(calls) == 1 and calls[0].method == "POST" and calls[0].url.path.endswith("/drafts")
    payload = json.loads(calls[0].content)
    assert payload["message"]["threadId"] == "t1"
    raw = payload["message"]["raw"]
    text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "To: person6@demodental.example" in text and "Cc: x@y.z" in text
    assert "Subject: Re: Invoice for October" in text and "In-Reply-To: <abc@mail>" in text
    assert "Attached." in text
    # Nothing in the connector can hit /send.
    assert not any("send" in str(r.url) for r in calls)


async def test_create_draft_error_surfaces():
    c = _gmail(httpx.MockTransport(lambda r: httpx.Response(403, text="insufficient scope")))
    with pytest.raises(gmail.ConnectorError):
        await c.create_draft("a@b.c", "s", "b")


def _thread(tid: str, last_sent: bool, age_days: int, to="client@x.com", subject="Proposal"):
    when = int((datetime.now(UTC) - timedelta(days=age_days)).timestamp() * 1000)
    msgs = [{"id": tid + "-1", "internalDate": str(when - 1000), "labelIds": ["INBOX"],
             "payload": {"headers": [{"name": "From", "value": "client@x.com"}]}}]
    msgs.append({
        "id": tid + "-2", "internalDate": str(when),
        "labelIds": ["SENT"] if last_sent else ["INBOX"],
        "payload": {"headers": [{"name": "To", "value": to}, {"name": "Subject", "value": subject},
                                {"name": "From", "value": "me@x.com"}]},
    })
    return {"id": tid, "messages": msgs}


async def test_waiting_for_finds_unanswered_sent_threads_and_signals_once():
    threads = {
        "t-old": _thread("t-old", True, 5),          # waiting 5 days: yes
        "t-fresh": _thread("t-fresh", True, 1),      # sent yesterday: not yet
        "t-answered": _thread("t-answered", False, 6),  # they replied: no
    }
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        p = req.url.path
        if p.endswith("/threads"):
            return httpx.Response(200, json={"threads": [{"id": k} for k in threads]})
        tid = p.rsplit("/", 1)[-1]
        return httpx.Response(200, json=threads[tid])

    c = _gmail(httpx.MockTransport(handler))
    items = await c.waiting_for(days=3)
    assert [i["thread_id"] for i in items] == ["t-old"]
    assert items[0]["to"] == "client@x.com" and items[0]["days_waiting"] == 5
    assert "in:sent" in calls[0].url.params["q"]

    assert await c.sync_waiting_for() == 1
    rows = [r for r in await signals.list_signals("new", limit=100) if r["source"] == "gmail"]
    assert rows[0]["subject"] == "No reply yet from client@x.com on: Proposal"
    assert rows[0]["raw"]["type"] == "waiting_for"
    assert rows[0]["url"].endswith("#all/t-old")
    # Rate-limited: a second call inside six hours does nothing.
    n_calls = len(calls)
    assert await c.sync_waiting_for() == 0
    assert len(calls) == n_calls
    assert "waiting_checked_at" in await state.get_cursor("gmail")


async def test_create_event_and_upcoming(monkeypatch):
    monkeypatch.setattr(gcal.settings, "timezone", "America/Chicago")
    calls: list[httpx.Request] = []
    start = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://cal/ev1",
                                             "start": body["start"], "end": body["end"]})
        return httpx.Response(200, json={"items": [
            {"id": "a", "summary": "Standup", "status": "confirmed",
             "start": {"dateTime": "2026-10-09T09:00:00-05:00"}, "end": {"dateTime": "2026-10-09T09:15:00-05:00"},
             "location": "Zoom", "htmlLink": "https://cal/a"},
            {"id": "b", "summary": "Gone", "status": "cancelled", "start": {"date": "2026-10-10"}},
            {"id": "c", "summary": "Holiday", "status": "confirmed", "start": {"date": "2026-10-12"}, "end": {"date": "2026-10-13"}},
        ]})

    c = _gcal(httpx.MockTransport(handler))
    out = await c.create_event("Demo Dental review", start, start + timedelta(hours=1),
                               description="Portal walkthrough", attendees=["person6@demodental.example"])
    assert out["event_id"] == "ev1" and out["url"] == "https://cal/ev1"
    body = json.loads(calls[0].content)
    assert body["summary"] == "Demo Dental review" and body["start"]["timeZone"] == "America/Chicago"
    assert body["attendees"] == [{"email": "person6@demodental.example"}]
    with pytest.raises(ValueError):
        await c.create_event("x", start, start)

    events = await c.upcoming(days=7)
    assert [e["summary"] for e in events] == ["Standup", "Holiday"]
    assert events[0]["start"].startswith("2026-10-09T09:00:00-05:00") and events[0]["all_day"] is False
    assert events[1]["all_day"] is True and events[1]["start"].startswith("2026-10-12T00:00:00")
    assert calls[-1].url.params["singleEvents"] == "true" and calls[-1].url.params["orderBy"] == "startTime"
