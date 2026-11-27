"""Tests for the PWA routes (core/web/pwa.py) over an in-process ASGI client
on the live test database. No push, no email, no model calls: senders and
the heavy handlers are patched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, projects, signals
from core.surfaces import notify
from core.web.api import app
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")
AGENT = "test_pwa_agent"
EP = "https://push.example/pwa"


class _Push:
    async def __call__(self, sub, payload, urgency) -> int:
        return 201


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Captures land as signals beside the real ones, and every push marks every
    # subscription used; both tables are private copies here. Notifications
    # stay real (approvals and briefs point at them).
    notify.set_senders(push=_Push())
    async with livedb.shadow(monkeypatch, "signals", "push_subscriptions"):
        yield
        notify.set_senders()
        await db.execute("delete from approvals where agent_name = $1", AGENT)
        await db.execute("delete from briefs where body like 'test-pwa%'")
        await db.execute("delete from notifications where kind like 'test-%'")
        await db.execute("delete from projects where slug like 'test-%'")


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


async def test_manifest_and_service_worker_served_at_root(client):
    r = await client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    assert r.json()["display"] == "standalone" and r.json()["start_url"] == "/"
    r = await client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["service-worker-allowed"] == "/"
    assert "javascript" in r.headers["content-type"]
    assert "notificationclick" in r.text and "showNotification" in r.text
    r = await client.get("/static/icons/icon-192.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_push_subscribe_requires_config_then_stores_and_removes(client, monkeypatch):
    monkeypatch.setattr(notify.settings, "vapid_public_key", "")
    r = await client.get("/api/push/vapid-public-key")
    assert r.json() == {"key": "", "configured": False}
    body = {"endpoint": EP + "/1", "keys": {"p256dh": "k", "auth": "a"}, "user_agent": "iOS"}
    assert (await client.post("/api/push/subscribe", json=body)).status_code == 503

    monkeypatch.setattr(notify.settings, "vapid_public_key", "pub")
    monkeypatch.setattr(notify.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(notify.settings, "vapid_subject", "mailto:t@example.com")
    r = await client.post("/api/push/subscribe", json=body)
    assert r.status_code == 200 and r.json()["count"] >= 1
    assert (await client.post("/api/push/subscribe", json={**body, "endpoint": "http://nope"})).status_code == 400
    r = await client.request("DELETE", "/api/push/subscribe", json={"endpoint": EP + "/1"})
    assert r.status_code == 200 and r.json()["removed"] is True
    r = await client.post("/api/push/test")
    assert r.status_code == 200 and "notification_id" in r.json()
    await db.execute("delete from notifications where kind = 'test'")


async def _pending_approval() -> str:
    return str(await db.fetchval(
        """
        insert into approvals (agent_name, action_summary, action_payload, status, expires_at)
        values ($1, 'send the invoice', '{"to": "a@b.com"}', 'pending', $2) returning id
        """,
        AGENT,
        datetime.now(UTC) + timedelta(hours=1),
    ))


async def test_approvals_list_get_decide_and_conflicts(client):
    aid = await _pending_approval()
    r = await client.get("/api/approvals/pending")
    assert aid in {a["id"] for a in r.json()["approvals"]}
    r = await client.get(f"/api/approvals/{aid}")
    assert r.status_code == 200 and r.json()["action_summary"] == "send the invoice"

    r = await client.post(f"/api/approvals/{aid}/decide", json={"decision": "approved"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = await client.post(f"/api/approvals/{aid}/decide", json={"decision": "rejected"})
    assert r.status_code == 409 and "already approved" in r.json()["detail"]
    assert (await client.post(f"/api/approvals/{uuid4()}/decide", json={"decision": "approved"})).status_code == 404
    assert (await client.post(f"/api/approvals/{aid}/decide", json={"decision": "maybe"})).status_code == 422
    assert (await client.get(f"/api/approvals/{uuid4()}")).status_code == 404


async def test_briefs_list_and_read(client):
    bid = await db.fetchval("insert into briefs (kind, body) values ('morning', 'test-pwa morning body') returning id")
    r = await client.get("/api/briefs?limit=5&kind=morning")
    assert str(bid) in {b["id"] for b in r.json()["briefs"]}
    r = await client.get(f"/api/briefs/{bid}")
    assert r.status_code == 200 and r.json()["read_at"] is not None
    assert (await client.get(f"/api/briefs/{uuid4()}")).status_code == 404


async def test_inbox_list_get_and_mark_read(client, monkeypatch):
    monkeypatch.setattr(notify.settings, "vapid_public_key", "")
    res = await notify.notify("test-inbox", "Hello", "body", data={"approval_id": "x"})
    assert res is not None
    nid = str(res.notification_id)
    r = await client.get("/api/notifications?unread=true&kind=test-inbox")
    assert nid in {n["id"] for n in r.json()["notifications"]}
    r = await client.get(f"/api/notifications/{nid}")
    assert r.status_code == 200 and r.json()["data"] == {"approval_id": "x"}
    r = await client.post("/api/notifications/read", json={"ids": [nid]})
    assert r.json() == {"marked": 1}
    r = await client.get("/api/notifications?unread=true&kind=test-inbox")
    assert nid not in {n["id"] for n in r.json()["notifications"]}


async def test_signals_and_projects(client):
    p = await projects.upsert_project("test-pwa", "PWA Project", repos=["x/y"])
    sid = await signals.insert_signal(signals.Signal(
        source="test-pwa", external_id="s1", occurred_at=datetime.now(UTC),
        subject="PR waits", project_id=p.id,
    ))
    await signals.set_triage(sid, urgency=2, importance=2, notes="PR #7 waits on review")
    r = await client.get("/api/signals?hours=24")
    mine = [s for s in r.json()["signals"] if s["id"] == str(sid)]
    assert mine and mine[0]["project_slug"] == "test-pwa" and mine[0]["triage_notes"] == "PR #7 waits on review"
    r = await client.get("/api/projects?status=active")
    row = [x for x in r.json()["projects"] if x["slug"] == "test-pwa"][0]
    assert row["repos"] == ["x/y"] and row["name"] == "PWA Project"


async def test_decision_review_and_learning_pursue(client):
    did = uuid4()
    with patch("core.web.pwa.record_review", new=AsyncMock(return_value=True)) as rec:
        r = await client.post(f"/api/decisions/{did}/review", json={"assessment": "right"})
    assert r.json() == {"recorded": True}
    rec.assert_awaited_once_with(did, "right", None)
    assert (await client.post(f"/api/decisions/{did}/review", json={"assessment": "meh"})).status_code == 422
    cid = uuid4()
    with patch("core.web.pwa.transition", new=AsyncMock(return_value=True)) as tr, \
            patch("core.learning_implement.draft_in_background"):
        r = await client.post(f"/api/learnings/{cid}/pursue")
    assert r.json() == {"moved": True, "drafting": True}
    tr.assert_awaited_once_with(cid, "pursuing", from_statuses=("proposed",))


async def test_share_to_learn_reads_the_tutorial_when_capabilities_are_on(client, monkeypatch):
    """The phone's share sheet feeds the full read (core/capabilities/), with
    the dashboard's cache policy; nothing here downloads or calls a model."""
    from core.web import capabilities as cap_web
    from core.web import pwa

    monkeypatch.setattr(pwa.settings, "ingest_token", "secret-token")
    monkeypatch.setattr(pwa.settings, "capabilities", True)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(cap_web, "_start", lambda sid, reason: started.append((sid, reason)))
    src = {"id": str(uuid4()), "status": "queued"}
    with patch("core.capabilities.pipeline.submit", new=AsyncMock(return_value={"source": src, "action": "new"})) as sub:
        r = await client.post("/api/ingest", json={"kind": "learn", "url": "https://youtu.be/AbCdEfGhIjK"},
                              headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json() == {"kind": "learn", "action": "new", "source_id": src["id"], "status": "queued"}
    sub.assert_awaited_once()
    assert started == [(src["id"], "first")]


async def test_ingest_auth_and_three_kinds(client, monkeypatch):
    from core.web import pwa

    monkeypatch.setattr(pwa.settings, "ingest_token", "")
    assert (await client.post("/api/ingest", json={"kind": "learn", "url": "https://x"})).status_code == 503
    monkeypatch.setattr(pwa.settings, "ingest_token", "secret-token")
    assert (await client.post("/api/ingest", json={"kind": "learn", "url": "https://x"})).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.post("/api/ingest", json={"kind": "learn", "url": "https://x"}, headers=bad)).status_code == 401
    ok = {"Authorization": "Bearer secret-token"}

    # The old one-sentence path, which share-to-learn uses while capabilities are off.
    monkeypatch.setattr(pwa.settings, "capabilities", False)
    with patch("core.loop.discovery_scan.ingest_url", new=AsyncMock(return_value={"outcome": "proposed"})) as ing:
        r = await client.post("/api/ingest", json={"kind": "learn", "text": "look https://instagram.com/reel/abc nice"}, headers=ok)
    assert r.status_code == 200 and r.json() == {"kind": "learn", "outcome": "proposed"}
    ing.assert_awaited_once_with("https://instagram.com/reel/abc")
    assert (await client.post("/api/ingest", json={"kind": "learn", "text": "no link here"}, headers=ok)).status_code == 400

    r = await client.post("/api/ingest", json={"kind": "capture", "text": "Call Chris about the HVAC invoice"}, headers=ok)
    assert r.status_code == 200 and r.json()["signal_id"] and r.json()["duplicate"] is False
    row = await signals.get_signal(r.json()["signal_id"])
    assert row is not None and row["source"] == "capture" and row["triage_status"] == "new"
    r = await client.post("/api/ingest", json={"kind": "capture", "text": "Call Chris about the HVAC invoice"}, headers=ok)
    assert r.json()["duplicate"] is True
    assert (await client.post("/api/ingest", json={"kind": "capture"}, headers=ok)).status_code == 400

    reply = SimpleNamespace(text="On it.", episode_id=uuid4())
    with patch("core.web.pwa.dispatch", new=AsyncMock(return_value=reply)) as disp:
        r = await client.post("/api/ingest", json={"kind": "ask", "text": "What is due today?"}, headers=ok)
    assert r.status_code == 200 and r.json()["reply"] == "On it."
    assert disp.await_args.args[:2] == ("ceo", "What is due today?")
