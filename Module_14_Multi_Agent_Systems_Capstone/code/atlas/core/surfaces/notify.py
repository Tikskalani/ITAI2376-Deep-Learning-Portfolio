"""Atlas -- the notification layer (replaces the Telegram surface).

notify(kind, title, body) is the only way the loop talks to Trilok. It:

  1. stores a notifications row (the PWA inbox and the audit trail)
  2. pushes to every push_subscriptions row (iOS PWA web push, VAPID)
  3. falls back to email when no push landed and SMTP is configured, or
     when priority is high

A sweep job, email_unread_notifications(), emails anything still unread
an hour after it was pushed, so a missed push never silently expires an
approval (revival plan, section 5.3). Email is a no-op when
ATLAS_SMTP_HOST is blank.

Kinds in use: brief/morning, brief/evening, brief/weekly, approval,
interrupt, reminder, workflow, health, decision_review, learning_digest,
learning_review. data carries ids the PWA needs (approval_id, brief_id,
signal_id, candidate ids) so a tap opens the right card.

Nothing here raises to the caller: a surface failure is logged and the
row stays in the inbox.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from core import db
from core.config import settings
from core.surfaces import webpush

log = logging.getLogger(__name__)

PRIORITIES = ("low", "normal", "high")
_PUSH_BODY_CHARS = 1000
_FALLBACK_AFTER = timedelta(minutes=60)

PushSender = Callable[[dict[str, Any], dict[str, Any], str], Awaitable[int]]
EmailSender = Callable[[str, str], Awaitable[bool]]


class NotifyResult(BaseModel):
    notification_id: UUID
    pushed: int = 0
    push_failed: int = 0
    emailed: bool = False


# ---------------------------------------------------------------------------
# Backends (injectable for tests)
# ---------------------------------------------------------------------------


def push_configured() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key and settings.vapid_subject)


def email_configured() -> bool:
    return bool(settings.smtp_host and settings.notify_email)


async def _default_push(sub: dict[str, Any], payload: dict[str, Any], urgency: str) -> int:
    return await webpush.send_push(
        sub,
        payload,
        subject=settings.vapid_subject,
        private_key=settings.vapid_private_key,
        public_key=settings.vapid_public_key,
        urgency=urgency,
    )


def _send_email_sync(subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user or settings.notify_email
    msg["To"] = settings.notify_email
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    return True


async def _default_email(subject: str, body: str) -> bool:
    if not email_configured():
        return False
    return await asyncio.to_thread(_send_email_sync, subject, body)


_push_sender: PushSender = _default_push
_email_sender: EmailSender = _default_email


def set_senders(
    *, push: PushSender | None = None, email: EmailSender | None = None
) -> None:
    """Swap backends (tests, or a future SMS backend). None restores the default."""
    global _push_sender, _email_sender
    _push_sender = push or _default_push
    _email_sender = email or _default_email


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def add_subscription(
    endpoint: str, p256dh: str, auth: str, user_agent: str | None = None
) -> UUID:
    """Store or refresh a PushSubscription. Idempotent on endpoint."""
    if not endpoint.startswith("https://"):
        raise ValueError("push endpoint must be https")
    return await db.fetchval(
        """
        insert into push_subscriptions (endpoint, p256dh, auth, user_agent)
        values ($1, $2, $3, $4)
        on conflict (endpoint) do update set
          p256dh = excluded.p256dh, auth = excluded.auth,
          user_agent = excluded.user_agent, failures = 0
        returning id
        """,
        endpoint,
        p256dh,
        auth,
        (user_agent or "")[:300] or None,
    )


async def remove_subscription(endpoint: str) -> bool:
    res = await db.execute("delete from push_subscriptions where endpoint = $1", endpoint)
    return res.endswith("1")


async def list_subscriptions() -> list[dict[str, Any]]:
    rows = await db.fetch("select * from push_subscriptions order by created_at")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _push_payload(nid: UUID, kind: str, title: str, body: str, priority: str, data: dict) -> dict:
    return {
        "id": str(nid),
        "kind": kind,
        "title": title[:120],
        "body": body[:_PUSH_BODY_CHARS],
        "priority": priority,
        "data": data,
    }


async def _push_all(nid: UUID, payload: dict[str, Any], priority: str) -> tuple[int, int]:
    """Push to every subscription. Returns (delivered, failed)."""
    if not push_configured():
        log.info("push not configured (VAPID keys missing); skipping push for %s", nid)
        return 0, 0
    subs = await list_subscriptions()
    urgency = "high" if priority == "high" else "normal" if priority == "normal" else "low"
    ok = failed = 0
    for sub in subs:
        try:
            await _push_sender(sub, payload, urgency)
        except webpush.SubscriptionGone:
            log.info("push subscription gone; removing %s", sub["endpoint"][:60])
            await db.execute("delete from push_subscriptions where id = $1", sub["id"])
            failed += 1
        except Exception as exc:
            failed += 1
            log.warning("push to %s failed: %s", sub["endpoint"][:60], exc)
            await db.execute(
                "update push_subscriptions set failures = failures + 1 where id = $1", sub["id"]
            )
        else:
            ok += 1
            await db.execute(
                "update push_subscriptions set last_used_at = now(), failures = 0 where id = $1",
                sub["id"],
            )
    return ok, failed


async def _email(nid: UUID, title: str, body: str) -> bool:
    try:
        sent = await _email_sender(f"[Atlas] {title}", body or title)
    except Exception as exc:
        log.warning("email for %s failed: %s", nid, exc)
        return False
    return bool(sent)


async def _record_delivery(nid: UUID, via: list[str]) -> None:
    if via:
        await db.execute(
            "update notifications set delivered_via = delivered_via || $2 where id = $1",
            nid,
            via,
        )


async def notify(
    kind: str,
    title: str,
    body: str = "",
    *,
    priority: str = "normal",
    data: dict[str, Any] | None = None,
) -> NotifyResult | None:
    """Tell Trilok something. Never raises; returns None only if the
    notification row itself could not be written."""
    if priority not in PRIORITIES:
        priority = "normal"
    data = data or {}
    try:
        nid: UUID = await db.fetchval(
            """
            insert into notifications (kind, title, body, priority, data)
            values ($1, $2, $3, $4, $5) returning id
            """,
            kind[:50],
            title[:300],
            body,
            priority,
            data,
        )
    except Exception:
        log.exception("could not store notification %s: %s", kind, title[:80])
        return None

    from core import events

    await events.emit("notified", title=title, detail=body,
                      data={"kind": kind, "priority": priority, "notification_id": str(nid)})
    result = NotifyResult(notification_id=nid)
    via: list[str] = []
    try:
        result.pushed, result.push_failed = await _push_all(
            nid, _push_payload(nid, kind, title, body, priority, data), priority
        )
    except Exception:
        log.exception("push stage failed for %s", nid)
    if result.pushed:
        via.append("push")

    if email_configured() and (result.pushed == 0 or priority == "high"):
        result.emailed = await _email(nid, title, body)
        if result.emailed:
            via.append("email")

    if not via:
        log.warning("notification %s (%s) reached no surface; it waits in the inbox", nid, kind)
    try:
        await _record_delivery(nid, via)
    except Exception:
        log.exception("could not record delivery for %s", nid)
    return result


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


async def list_notifications(
    *, unread_only: bool = True, limit: int = 50, kind: str | None = None
) -> list[dict[str, Any]]:
    clauses = []
    args: list[Any] = []
    if unread_only:
        clauses.append("read_at is null")
    if kind:
        args.append(kind)
        clauses.append(f"kind = ${len(args)}")
    args.append(limit)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = await db.fetch(
        f"select * from notifications {where} order by created_at desc limit ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]


async def get_notification(notification_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow("select * from notifications where id = $1", notification_id)
    return dict(row) if row else None


async def mark_read(ids: list[UUID]) -> int:
    if not ids:
        return 0
    res = await db.execute(
        "update notifications set read_at = now() where id = any($1) and read_at is null",
        ids,
    )
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def email_unread_notifications(*, older_than: timedelta = _FALLBACK_AFTER) -> int:
    """Scheduler job: email what has sat unread past the window. Returns
    how many were emailed. No-op without SMTP."""
    if not email_configured():
        return 0
    cutoff = datetime.now(UTC) - older_than
    rows = await db.fetch(
        """
        select id, title, body from notifications
        where read_at is null and created_at < $1
          and priority <> 'low'
          and not ('email' = any(delivered_via))
        order by created_at
        limit 20
        """,
        cutoff,
    )
    sent = 0
    for r in rows:
        if await _email(r["id"], r["title"], r["body"]):
            await _record_delivery(r["id"], ["email"])
            sent += 1
    if sent:
        log.info("emailed %d unread notification(s) past the fallback window", sent)
    return sent


__all__ = [
    "PRIORITIES",
    "NotifyResult",
    "notify",
    "push_configured",
    "email_configured",
    "set_senders",
    "add_subscription",
    "remove_subscription",
    "list_subscriptions",
    "list_notifications",
    "get_notification",
    "mark_read",
    "email_unread_notifications",
]
