"""Tests for core/surfaces/notify.py on the live test database with the
push and email senders replaced by fakes. No network, no SMTP."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio

from core import db
from core.surfaces import notify, webpush
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")

EP = "https://push.example/test/"


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Pushes go to every subscription and the sweep scans every notification;
    # on the live database the real ones must be neither counted nor touched.
    async with livedb.shadow(monkeypatch, "notifications", "push_subscriptions"):
        yield
        notify.set_senders()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(notify.settings, "vapid_public_key", "pub")
    monkeypatch.setattr(notify.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(notify.settings, "vapid_subject", "mailto:t@example.com")
    monkeypatch.setattr(notify.settings, "smtp_host", "")
    monkeypatch.setattr(notify.settings, "notify_email", "")


class _Push:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict, str]] = []
        self.fail: dict[str, Exception] = {}

    async def __call__(self, sub, payload, urgency) -> int:
        self.calls.append((sub, payload, urgency))
        exc = self.fail.get(sub["endpoint"])
        if exc:
            raise exc
        return 201


class _Email:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    async def __call__(self, subject, body) -> bool:
        self.sent.append((subject, body))
        return self.ok


async def test_subscriptions_upsert_on_endpoint_and_require_https():
    a = await notify.add_subscription(EP + "a", "k1", "a1", "Safari iOS")
    again = await notify.add_subscription(EP + "a", "k2", "a2")
    assert a == again
    subs = [s for s in await notify.list_subscriptions() if s["endpoint"].startswith(EP)]
    assert len(subs) == 1 and subs[0]["p256dh"] == "k2" and subs[0]["user_agent"] is None
    with pytest.raises(ValueError):
        await notify.add_subscription("http://insecure", "k", "a")
    assert await notify.remove_subscription(EP + "a") is True
    assert await notify.remove_subscription(EP + "a") is False


async def test_notify_pushes_to_every_subscription_and_prunes_dead_ones(configured):
    await notify.add_subscription(EP + "phone", "k", "a")
    await notify.add_subscription(EP + "laptop", "k", "a")
    await notify.add_subscription(EP + "dead", "k", "a")
    await notify.add_subscription(EP + "flaky", "k", "a")
    push = _Push()
    push.fail[EP + "dead"] = webpush.SubscriptionGone("410")
    push.fail[EP + "flaky"] = webpush.WebPushError("503")
    notify.set_senders(push=push)

    res = await notify.notify("test-brief", "Morning brief", "x" * 3000, priority="high",
                              data={"brief_id": "b1"})
    assert res is not None
    assert res.pushed == 2 and res.push_failed == 2 and res.emailed is False
    assert len(push.calls) == 4
    sub, payload, urgency = push.calls[0]
    assert urgency == "high"
    assert payload["title"] == "Morning brief" and len(payload["body"]) == 1000
    assert payload["data"] == {"brief_id": "b1"} and UUID(payload["id"]) == res.notification_id

    endpoints = {s["endpoint"] for s in await notify.list_subscriptions()}
    assert EP + "dead" not in endpoints and EP + "flaky" in endpoints
    flaky = await db.fetchrow("select failures from push_subscriptions where endpoint = $1", EP + "flaky")
    assert flaky is not None and flaky["failures"] == 1
    row = await notify.get_notification(res.notification_id)
    assert row is not None
    assert row["delivered_via"] == ["push"] and row["read_at"] is None
    assert len(row["body"]) == 3000  # the inbox keeps the full text


async def test_email_fallback_when_nothing_pushed_and_for_high_priority(configured, monkeypatch):
    monkeypatch.setattr(notify.settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(notify.settings, "notify_email", "t@example.com")
    push = _Push()
    email = _Email()
    notify.set_senders(push=push, email=email)

    # No subscriptions at all: email carries it.
    res = await notify.notify("test-alert", "Loop quiet", "details")
    assert res is not None and res.pushed == 0 and res.emailed is True
    assert email.sent[-1] == ("[Atlas] Loop quiet", "details")
    row = await notify.get_notification(res.notification_id)
    assert row is not None and row["delivered_via"] == ["email"]

    # With a working subscription, normal priority is push only...
    await notify.add_subscription(EP + "phone2", "k", "a")
    res = await notify.notify("test-note", "Normal", "n")
    assert res is not None and res.pushed == 1 and res.emailed is False
    # ...and high priority goes both ways.
    res = await notify.notify("test-note", "Urgent", "u", priority="high")
    assert res is not None and res.pushed == 1 and res.emailed is True
    row = await notify.get_notification(res.notification_id)
    assert row is not None and sorted(row["delivered_via"]) == ["email", "push"]


async def test_notify_without_push_config_still_stores_and_warns(monkeypatch):
    monkeypatch.setattr(notify.settings, "vapid_private_key", "")
    monkeypatch.setattr(notify.settings, "smtp_host", "")
    push = _Push()
    notify.set_senders(push=push)
    res = await notify.notify("test-unconfigured", "Hello")
    assert res is not None and res.pushed == 0 and push.calls == []
    row = await notify.get_notification(res.notification_id)
    assert row is not None and row["delivered_via"] == []


async def test_inbox_listing_and_mark_read(configured):
    notify.set_senders(push=_Push())
    a = await notify.notify("test-inbox", "A")
    b = await notify.notify("test-inbox", "B")
    assert a and b
    unread = [n["id"] for n in await notify.list_notifications(kind="test-inbox")]
    assert unread[:2] == [b.notification_id, a.notification_id]  # newest first
    assert await notify.mark_read([a.notification_id]) == 1
    assert await notify.mark_read([a.notification_id]) == 0  # already read
    assert [n["id"] for n in await notify.list_notifications(kind="test-inbox")] == [b.notification_id]
    everything = await notify.list_notifications(unread_only=False, kind="test-inbox")
    assert {n["id"] for n in everything} >= {a.notification_id, b.notification_id}
    assert await notify.mark_read([]) == 0


async def test_email_unread_sweep_targets_only_old_unread_unemailed(configured, monkeypatch):
    monkeypatch.setattr(notify.settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(notify.settings, "notify_email", "t@example.com")
    await notify.add_subscription(EP + "phone3", "k", "a")
    email = _Email()
    notify.set_senders(push=_Push(), email=email)

    old = await notify.notify("test-sweep", "Old unread", "body")
    read = await notify.notify("test-sweep", "Old read", "body")
    low = await notify.notify("test-sweep", "Old low", "body", priority="low")
    fresh = await notify.notify("test-sweep", "Fresh", "body")
    assert old and read and low and fresh
    assert email.sent == []  # all pushed fine, nothing emailed yet
    for nid in (old.notification_id, read.notification_id, low.notification_id):
        await db.execute(
            "update notifications set created_at = now() - interval '2 hours' where id = $1", nid
        )
    await notify.mark_read([read.notification_id])

    sent = await notify.email_unread_notifications(older_than=timedelta(minutes=60))
    assert sent == 1
    assert email.sent == [("[Atlas] Old unread", "body")]
    row = await notify.get_notification(old.notification_id)
    assert row is not None and "email" in row["delivered_via"]
    # Second sweep: nothing left to send.
    assert await notify.email_unread_notifications(older_than=timedelta(minutes=60)) == 0

    monkeypatch.setattr(notify.settings, "smtp_host", "")
    assert await notify.email_unread_notifications() == 0
