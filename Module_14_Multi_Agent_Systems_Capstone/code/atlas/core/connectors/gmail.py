"""Atlas -- Gmail connector.

Reads new mail into signals. First sync lists the last two days of mail;
after that it walks users.history from the stored historyId, which is
cheap and exact. Each new message becomes one signal: sender as actor,
subject, the text/plain body (capped) or the snippet, a link that opens
the thread. Promotions, social, spam, trash, drafts and the owner's own
sent mail are skipped; triage decides the rest.

The privacy gate (core/privacy.py) runs first, before the noise list and
the known-people list, which it beats: a message from an excluded sender,
domain, Gmail label or To/Cc domain becomes a private signal with no body
and the subject "Private message (<rule label>)". Label names come from
users.labels.list, cached for an hour; if they cannot be read while label
rules exist, any message with a user label is treated as private.

Scope (from core/connect.py): gmail.readonly only. create_draft needs
gmail.compose, which Atlas no longer requests, so it fails with a clear
message; drafts also need an approval card (migration 035).

Cursor (connector_state, source "gmail"): {"history_id": "..."}. A 404
on history means the id is older than Gmail keeps; the connector falls
back to the recent list and continues.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx

from core import privacy
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

SOURCE = "gmail"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = httpx.Timeout(20.0)
_MAX_MESSAGES_PER_SYNC = 60
_BODY_CHARS = 4000
_SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT", "SENT"}
_RECENT_QUERY = "newer_than:2d -category:promotions -category:social"
_LABEL_TTL = 3600.0
_labels: dict[str, Any] = {"at": 0.0, "names": None}
UNCHECKED_LABEL = "unchecked Gmail label"

ClientFactory = Callable[[], httpx.AsyncClient]


class HistoryGone(ConnectorError):
    """The stored historyId is too old; do a fresh listing."""


def _b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def extract_text(payload: dict[str, Any]) -> str:
    """The first text/plain part of a message payload, decoded."""
    if not payload:
        return ""
    mime = payload.get("mimeType") or ""
    body = payload.get("body") or {}
    if mime == "text/plain" and body.get("data"):
        return _b64url(body["data"])
    for part in payload.get("parts") or []:
        text = extract_text(part)
        if text:
            return text
    if mime == "text/html" and body.get("data"):
        from core.connectors.canvas import strip_html

        return strip_html(_b64url(body["data"]))
    return ""


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        name = (h.get("name") or "").lower()
        if name in ("from", "to", "subject", "date", "cc"):
            out[name] = h.get("value") or ""
    return out


def private_match(msg: dict[str, Any], label_names: dict[str, str] | None) -> privacy.Match | None:
    """The privacy rule a message matches, before anything else looks at it."""
    headers = _headers(msg)
    ids = list(msg.get("labelIds") or [])
    names = [(label_names or {}).get(i, i) for i in ids]
    hit = privacy.is_private_mail(headers.get("from", ""), recipients=[headers.get("to", ""), headers.get("cc", "")],
                                  labels=names)
    if hit is None and label_names is None and any(i.startswith("Label_") for i in ids) \
            and any(r.labels for r in privacy.load().rules):
        # Label names unknown and label rules exist: fail closed.
        hit = privacy.Match(UNCHECKED_LABEL, "label")
    return hit


def message_to_signal(msg: dict[str, Any], label_names: dict[str, str] | None = None) -> Signal | None:
    """Map one users.messages.get(format=full) result to a Signal.
    label_names maps label ids to names (None when they could not be read)."""
    labels = set(msg.get("labelIds") or [])
    if labels & _SKIP_LABELS:
        return None
    headers = _headers(msg)
    hit = private_match(msg, label_names)
    if hit is not None:
        sender_name, sender_addr = parseaddr(headers.get("from", ""))
        thread = msg.get("threadId") or msg.get("id")
        try:
            when = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=UTC)
        except (TypeError, ValueError):
            when = datetime.now(UTC)
        return Signal(source=SOURCE, external_id=str(msg["id"]), occurred_at=when,
                      actor=sender_name or sender_addr or "unknown", subject=privacy.placeholder(hit.label),
                      body="", url=f"https://mail.google.com/mail/u/0/#all/{thread}",
                      raw={"type": "email", "thread_id": thread}, private=True, private_rule=hit.label)
    sender_name, sender_addr = parseaddr(headers.get("from", ""))
    # Senders Trilok never opens (config/context.yaml) never become signals,
    # so they cost no triage tokens and never reach a brief.
    from core import context_pack
    if context_pack.is_noise(headers.get("from", "")):
        return None
    known = context_pack.sender_hint(headers.get("from", ""))
    actor = sender_name or sender_addr or "unknown"
    try:
        occurred = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=UTC)
    except (TypeError, ValueError):
        occurred = datetime.now(UTC)
    text = extract_text(msg.get("payload") or {}).strip() or (msg.get("snippet") or "")
    thread = msg.get("threadId") or msg.get("id")
    return Signal(
        source=SOURCE,
        external_id=str(msg["id"]),
        occurred_at=occurred,
        actor=actor,
        subject=(headers.get("subject") or "(no subject)")[:300],
        body=text[:_BODY_CHARS],
        url=f"https://mail.google.com/mail/u/0/#all/{thread}",
        raw={
            "type": "email",
            "thread_id": thread,
            "from": sender_addr,
            "to": headers.get("to", "")[:200],
            "labels": sorted(labels)[:10],
            "known": known,
        },
    )


class GmailConnector(Connector):
    name = SOURCE
    cache_ttl_seconds = 0.0
    max_attempts = 3

    def __init__(
        self,
        token_provider: TokenProvider | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self._token = token_provider or default_token_provider
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(base_url=API, timeout=_TIMEOUT))
        self._current_token: str | None = None

    async def _bearer(self) -> str:
        # The token is fetched once per sync, outside the base class's retry
        # wrapper, so a consent failure keeps its type instead of becoming a
        # generic ConnectorError. A 401 clears it and the retry re-fetches.
        if self._current_token is None:
            self._current_token = await self._token()
        return self._current_token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._bearer()
        async with self._client_factory() as client:
            try:
                resp = await client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise TransientConnectorError(f"gmail {path}: {exc}") from exc
        if resp.status_code == 401:
            invalidate()
            self._current_token = None
            raise TransientConnectorError(f"gmail {path}: token rejected; refreshed for retry")
        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientConnectorError(f"gmail {path}: HTTP {resp.status_code}")
        if resp.status_code == 404 and path.startswith("/history"):
            raise HistoryGone("gmail history id is too old")
        if resp.status_code >= 400:
            raise ConnectorError(f"gmail {path}: HTTP {resp.status_code} {resp.text[:120]}")
        return resp.json()

    async def _fetch_fresh(self, key: str) -> Any:
        kind, _, arg = key.partition(":")
        if kind == "profile":
            return await self._get("/profile")
        if kind == "recent":
            return await self._get("/messages", {"q": _RECENT_QUERY, "maxResults": 50})
        if kind == "history":
            ids: list[str] = []
            page: str | None = None
            newest = arg
            for _ in range(5):
                params: dict[str, Any] = {
                    "startHistoryId": arg, "historyTypes": "messageAdded", "maxResults": 200,
                }
                if page:
                    params["pageToken"] = page
                data = await self._get("/history", params)
                newest = data.get("historyId") or newest
                for h in data.get("history") or []:
                    for added in h.get("messagesAdded") or []:
                        mid = (added.get("message") or {}).get("id")
                        if mid and mid not in ids:
                            ids.append(mid)
                page = data.get("nextPageToken")
                if not page:
                    break
            return {"ids": ids, "historyId": newest}
        if kind == "message":
            return await self._get(f"/messages/{arg}", {"format": "full"})
        if kind == "labels":
            return await self._get("/labels")
        raise ConnectorError(f"gmail: unknown key {key!r}")

    async def label_names(self) -> dict[str, str] | None:
        """{label id: name}, cached for an hour; None when they cannot be read."""
        now = datetime.now(UTC).timestamp()
        if _labels["names"] is not None and now - _labels["at"] < _LABEL_TTL:
            return _labels["names"]  # type: ignore[no-any-return]
        try:
            data = (await self.get("labels", allow_stale=False)).data
        except Exception as exc:
            log.warning("gmail connector: label names unavailable: %s", type(exc).__name__)
            return None
        names = {str(x.get("id")): str(x.get("name") or "") for x in data.get("labels") or []}
        _labels.update(at=now, names=names)
        return names

    async def sync(self) -> dict[str, Any]:
        """Pull new mail into signals. Never raises."""
        summary: dict[str, Any] = {"messages": 0, "new_signals": 0, "failures": []}
        if not configured():
            log.info("gmail connector: google oauth not configured; skipping")
            return summary
        cursor = await state.get_cursor(SOURCE)
        history_id = cursor.get("history_id")
        self._current_token = None
        try:
            await self._bearer()
            if history_id:
                try:
                    hist = (await self.get(f"history:{history_id}", allow_stale=False)).data
                    ids, new_history = hist["ids"], hist["historyId"]
                except (HistoryGone, ConnectorError) as exc:
                    if not isinstance(exc, HistoryGone) and "too old" not in str(exc):
                        raise
                    log.info("gmail connector: history too old; re-listing recent mail")
                    history_id = None
            if not history_id:
                recent = (await self.get("recent", allow_stale=False)).data
                ids = [m["id"] for m in recent.get("messages") or []]
                profile = (await self.get("profile", allow_stale=False)).data
                new_history = profile.get("historyId")
        except GoogleNeedsConsent as exc:
            await self._needs_consent(exc)
            summary["failures"].append("auth")
            return summary
        except Exception as exc:
            log.warning("gmail connector: listing failed: %s", exc)
            await state.record_failure(SOURCE, f"list: {exc}")
            summary["failures"].append("list")
            return summary

        label_names = await self.label_names()
        for mid in ids[:_MAX_MESSAGES_PER_SYNC]:
            summary["messages"] += 1
            try:
                msg = (await self.get(f"message:{mid}", allow_stale=False)).data
            except Exception as exc:
                log.warning("gmail connector: message %s failed: %s", mid, exc)
                summary["failures"].append(mid)
                continue
            sig = message_to_signal(msg, label_names)
            if sig is None:
                continue
            if await insert_signal(sig) is not None:
                summary["new_signals"] += 1
        if new_history:
            cursor["history_id"] = str(new_history)
            await state.set_cursor(SOURCE, cursor)
        else:
            await state.record_ok(SOURCE)
        summary["waiting_for"] = await self.sync_waiting_for()
        return summary

    # -- hands: drafts (never send) and "waiting for" detection ------------------

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        cc: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a Gmail draft. Atlas never sends; Trilok opens the draft
        in Gmail and decides. Returns {draft_id, message_id, thread_id, url}."""
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        token = await self._bearer()
        async with self._client_factory() as client:
            try:
                resp = await client.post(
                    "/drafts", json=payload, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise TransientConnectorError(f"gmail drafts: {exc}") from exc
        if resp.status_code == 403:
            raise ConnectorError(
                "Google is connected read-only (docs/privacy.md), so Atlas cannot create Gmail drafts. "
                "Copy the draft into Gmail yourself.")
        if resp.status_code >= 400:
            raise ConnectorError(f"gmail drafts: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        message = data.get("message") or {}
        return {
            "draft_id": data.get("id"),
            "message_id": message.get("id"),
            "thread_id": message.get("threadId") or thread_id,
            "url": "https://mail.google.com/mail/u/0/#drafts",
        }

    async def waiting_for(self, *, days: int = 3, lookback_days: int = 21) -> list[dict[str, Any]]:
        """Threads where the last message is one Trilok sent and nobody has
        answered for `days`. Inferred from mail, never typed in.

        Returns [{thread_id, message_id, to, subject, sent_at, days_waiting}].
        """
        listing = await self._get(
            "/threads", {"q": f"in:sent newer_than:{lookback_days}d", "maxResults": 40}
        )
        out: list[dict[str, Any]] = []
        cutoff = datetime.now(UTC) - timedelta(days=days)
        for t in listing.get("threads") or []:
            thread = await self._get(
                f"/threads/{t['id']}",
                {"format": "metadata", "metadataHeaders": ["From", "To", "Subject"]},
            )
            messages = sorted(
                thread.get("messages") or [], key=lambda m: int(m.get("internalDate") or 0)
            )
            if not messages:
                continue
            last = messages[-1]
            labels = set(last.get("labelIds") or [])
            if "SENT" not in labels or "DRAFT" in labels:
                continue
            try:
                sent_at = datetime.fromtimestamp(int(last.get("internalDate", 0)) / 1000, tz=UTC)
            except (TypeError, ValueError):
                continue
            if sent_at > cutoff:
                continue
            headers = _headers(last)
            out.append(
                {
                    "thread_id": t["id"],
                    "message_id": last.get("id"),
                    "to": headers.get("to", "")[:200],
                    "subject": (headers.get("subject") or "(no subject)")[:200],
                    "sent_at": sent_at.isoformat(),
                    "days_waiting": (datetime.now(UTC) - sent_at).days,
                }
            )
        return out

    async def sync_waiting_for(self) -> int:
        """Turn waiting-for threads into signals, at most every six hours.
        Returns how many new signals were written. Never raises."""
        try:
            cursor = await state.get_cursor(SOURCE)
            last = cursor.get("waiting_checked_at")
            if last:
                checked = datetime.fromisoformat(last)
                if datetime.now(UTC) - checked < timedelta(hours=6):
                    return 0
            items = await self.waiting_for()
        except Exception as exc:
            log.warning("gmail connector: waiting-for check failed: %s", exc)
            return 0
        new = 0
        for item in items:
            sig = Signal(
                source=SOURCE,
                external_id=f"waiting:{item['thread_id']}:{item['message_id']}",
                occurred_at=datetime.now(UTC),
                actor="trilok",
                subject=f"No reply yet from {item['to'] or 'them'} on: {item['subject']}",
                body=f"Sent {item['sent_at'][:10]}, {item['days_waiting']} days ago. A nudge may be due.",
                url=f"https://mail.google.com/mail/u/0/#all/{item['thread_id']}",
                raw={"type": "waiting_for", **item},
            )
            if await insert_signal(sig) is not None:
                new += 1
        cursor = await state.get_cursor(SOURCE)
        cursor["waiting_checked_at"] = datetime.now(UTC).isoformat()
        await state.set_cursor(SOURCE, cursor)
        return new

    async def _needs_consent(self, exc: Exception) -> None:
        """Record the auth failure and tell the owner once a day."""
        prev = await state.get_state(SOURCE)
        n = await state.record_failure(SOURCE, f"auth: {exc}")
        last_err = prev.get("last_error") or ""
        updated = prev.get("updated_at")
        recently = (
            last_err.startswith("auth:")
            and updated is not None
            and (datetime.now(UTC) - updated).total_seconds() < 24 * 3600
        )
        if recently:
            return
        try:
            from core.surfaces.notify import notify

            await notify(
                kind="health",
                title="Google sign-in expired",
                body="Gmail and Calendar stopped syncing: Google rejected the refresh token (a project in "
                "Testing mode expires them after 7 days). On the laptop, open Atlas > Connect and tap "
                "Reconnect Google.",
                priority="high",
            )
        except Exception:
            log.exception("gmail connector: could not notify about expired consent")
        log.warning("gmail connector: google consent needed (failure #%d)", n)


__all__ = ["GmailConnector", "message_to_signal", "extract_text", "SOURCE"]
