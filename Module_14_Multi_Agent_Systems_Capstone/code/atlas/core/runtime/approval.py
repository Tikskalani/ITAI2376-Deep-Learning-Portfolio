"""Atlas -- the approval broker.

Every irreversible action (money movement, sent messages, public posts,
submitted forms, account changes) passes through here: an agent prepares
the action, Trilok decides on a card in the PWA, and only then does it
run. The approval_policy table decides which tool calls are gated; this
module runs the round trip.

  request_approval   write the approvals row, notify() the owner with a
                     card, await the decision (an asyncio.Future keyed by
                     approval id) up to a wait window
  decide_approval    the PWA's POST /api/approvals/{id}/decide lands
                     here; resolves the Future when the requester is
                     still waiting, and records the decision either way
  expire_overdue     scheduler job that marks pending rows past their
                     TTL expired and wakes any waiter
  check_approval     non-blocking read
  execute_on_approval run an executor only on an approved outcome

Two time windows. expires_in_hours is the TTL on the row: after it, the
card is dead. wait_seconds is how long the requester blocks; the policy
gate in the tool loop waits minutes, not hours, so a specialist running
inside the executive tick cannot stall the scheduler. A decision that
arrives after the requester stopped waiting is still recorded (status
approved or rejected, decided_by set) and acted on: core.runtime.late
runs the approved tool call itself, once, under its ledger key, and R4's
durable workflow runs pick up their own.

The Future lives in process memory only; a worker restart while a card
is open leaves the row pending until decided or expired, which is the
right outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from core import db
from core.config import settings
from core.surfaces.notify import notify

log = logging.getLogger(__name__)

# An executor takes the approved payload and performs the real action.
Executor = Callable[[dict[str, Any]], Awaitable[Any]]

DECISIONS = ("approved", "rejected")

# Waiters keyed by approval id. decide_approval and expire_overdue resolve them.
_pending: dict[UUID, asyncio.Future[tuple[str, dict[str, Any] | None]]] = {}


@dataclass
class ApprovalOutcome:
    """The result of an approval request.

    status is 'approved', 'rejected', 'expired', or 'pending' (the wait
    window closed before a decision; the card is still open). payload
    carries the approved action back to the caller only when approved.
    """

    status: str
    payload: dict[str, Any] | None
    approval_id: UUID | None = None

    @property
    def approved(self) -> bool:
        return self.status == "approved"


def _card_body(summary: str, payload: dict[str, Any], agent_name: str, expires_at: datetime) -> str:
    try:
        shown = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        shown = str(payload)
    if len(shown) > 1500:
        shown = shown[:1500] + "\n..."
    return (
        f"{agent_name} wants to: {summary}\n\n{shown}\n\n"
        f"Expires {expires_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}."
    )


async def request_approval(
    *,
    summary: str,
    payload: dict[str, Any],
    agent_name: str,
    message: str | None = None,
    expires_in_hours: int | None = None,
    wait_seconds: float | None = None,
    episode_id: UUID | None = None,
    goal_id: UUID | None = None,
) -> ApprovalOutcome:
    """Raise a card and wait for the decision.

    summary is the one-line description on the card. message overrides
    the card body. expires_in_hours defaults to settings.approval_ttl_hours;
    wait_seconds defaults to the full TTL (the Telegram semantics) and the
    tool-loop gate passes a short window.
    """
    ttl_hours = expires_in_hours if expires_in_hours is not None else settings.approval_ttl_hours
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    wait = wait_seconds if wait_seconds is not None else ttl_hours * 3600

    approval_id: UUID = await db.fetchval(
        """
        insert into approvals (agent_name, action_summary, action_payload, status, expires_at)
        values ($1, $2, $3, 'pending', $4)
        returning id
        """,
        agent_name,
        summary[:500],
        payload,
        expires_at,
    )

    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, dict[str, Any] | None]] = loop.create_future()
    _pending[approval_id] = future
    from core import events

    await events.emit("approval.asked", agent=agent_name, episode_id=episode_id, title=summary,
                      data={"approval_id": str(approval_id), "expires_at": expires_at.isoformat()})

    res = await notify(
        "approval",
        f"Approve: {summary[:100]}",
        message or _card_body(summary, payload, agent_name, expires_at),
        priority="high",
        data={
            "approval_id": str(approval_id),
            "agent": agent_name,
            "expires_at": expires_at.isoformat(),
        },
    )
    if res is not None:
        await _link_notification(approval_id, res.notification_id)

    try:
        status, returned = await asyncio.wait_for(future, timeout=wait)
    except TimeoutError:
        if wait >= ttl_hours * 3600:
            await _expire_one(approval_id)
            status, returned = "expired", None
        else:
            status, returned = "pending", None
    finally:
        _pending.pop(approval_id, None)

    approved_by = "user" if status in DECISIONS else "auto"
    await db.execute(
        """
        insert into actions
          (agent_name, action_type, inputs, outputs, approved_by, episode_id, goal_id)
        values ($1, 'approval_request', $2, $3, $4, $5, $6)
        """,
        agent_name,
        {"summary": summary, "payload": payload, "ttl_hours": ttl_hours,
         "approval_id": str(approval_id)},
        {"status": status},
        approved_by,
        episode_id,
        goal_id,
    )
    log.info("approval %s for %s: %s", status, agent_name, summary[:80])
    return ApprovalOutcome(status=status, payload=returned, approval_id=approval_id)


async def open_approval(
    *,
    summary: str,
    payload: dict[str, Any],
    agent_name: str,
    message: str | None = None,
    expires_in_hours: int | None = None,
) -> UUID:
    """Raise a card without waiting. The caller (the workflow engine) polls
    the row's status later; a decision wakes nobody but is recorded."""
    ttl_hours = expires_in_hours if expires_in_hours is not None else settings.approval_ttl_hours
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    approval_id: UUID = await db.fetchval(
        """
        insert into approvals (agent_name, action_summary, action_payload, status, expires_at)
        values ($1, $2, $3, 'pending', $4)
        returning id
        """,
        agent_name,
        summary[:500],
        payload,
        expires_at,
    )
    res = await notify(
        "approval",
        f"Approve: {summary[:100]}",
        message or _card_body(summary, payload, agent_name, expires_at),
        priority="high",
        data={"approval_id": str(approval_id), "agent": agent_name, "expires_at": expires_at.isoformat()},
    )
    if res is not None:
        await _link_notification(approval_id, res.notification_id)
    await db.execute(
        """
        insert into actions (agent_name, action_type, inputs, outputs, approved_by)
        values ($1, 'approval_request', $2, $3, 'auto')
        """,
        agent_name,
        {"summary": summary, "payload": payload, "ttl_hours": ttl_hours, "approval_id": str(approval_id)},
        {"status": "pending"},
    )
    return approval_id


async def decide_approval(
    approval_id: UUID, decision: str, *, decided_by: str = "owner"
) -> dict[str, Any] | None:
    """Record Trilok's decision and wake the requester if it is still waiting.

    Returns the updated row, or None when the id is unknown or the row
    was already decided or expired (the PWA shows "already handled").
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    approval_id = _as_uuid(approval_id)
    row = await db.fetchrow(
        """
        update approvals
        set status = $2, resolved_at = now(), decided_by = $3
        where id = $1 and status = 'pending'
        returning id, agent_name, action_summary, action_payload, status,
                  resolved_at, notification_id
        """,
        approval_id,
        decision,
        decided_by,
    )
    if row is None:
        return None
    from core import events

    await events.emit("approval.decided", agent=row["agent_name"], title=row["action_summary"],
                      data={"approval_id": str(approval_id), "decision": decision, "by": decided_by})
    if row["notification_id"]:
        await db.execute(
            "update notifications set read_at = coalesce(read_at, now()) where id = $1",
            row["notification_id"],
        )
    future = _pending.get(approval_id)
    if future is not None and not future.done():
        future.set_result((decision, dict(row["action_payload"]) if decision == "approved" else None))
    else:
        # Nobody is waiting: the agent gave up minutes ago and reported what
        # it prepared. A yes still finishes the job; a no closes the ledger.
        log.info("approval %s decided (%s) after the requester stopped waiting", approval_id, decision)
        from core.runtime import late

        if decision == "approved":
            late.start(approval_id)
        else:
            await late.settle_rejected(approval_id)
    return dict(row)


async def _link_notification(approval_id: UUID, notification_id: Any) -> None:
    """Attach the card to its approval. A decision can land between the card
    going out and this link (decide_approval then saw no notification_id and
    could not mark it), so mark the card read here if the row is already decided."""
    await db.execute(
        "update approvals set notification_id = $2 where id = $1", approval_id, notification_id
    )
    await db.execute(
        """
        update notifications set read_at = now()
        where id = $2 and read_at is null
          and exists (select 1 from approvals where id = $1 and status <> 'pending')
        """,
        approval_id,
        notification_id,
    )


def _as_uuid(value: Any) -> UUID:
    """Ids arrive as UUIDs from code and as strings from the PWA and push data."""
    return value if isinstance(value, UUID) else UUID(str(value))


async def _expire_one(approval_id: UUID) -> bool:
    res = await db.execute(
        "update approvals set status = 'expired', resolved_at = now() "
        "where id = $1 and status = 'pending'",
        approval_id,
    )
    return res.endswith("1")


async def expire_overdue_approvals() -> int:
    """Scheduler job: expire pending rows past their TTL. Never raises."""
    try:
        rows = await db.fetch(
            """
            update approvals set status = 'expired', resolved_at = now()
            where status = 'pending' and expires_at < now()
            returning id, agent_name
            """
        )
    except Exception:
        log.exception("could not expire overdue approvals")
        return 0
    for r in rows:
        future = _pending.get(r["id"])
        if future is not None and not future.done():
            future.set_result(("expired", None))
    if rows:
        log.info("expired %d overdue approval(s)", len(rows))
        from core import events

        for r in rows:
            await events.emit("approval.expired", agent=r["agent_name"], title="expired", data={"approval_id": str(r["id"])})
    return len(rows)


async def check_approval(approval_id: UUID) -> dict[str, Any] | None:
    """Read the current state of an approval row. None if no such row."""
    approval_id = _as_uuid(approval_id)
    row = await db.fetchrow(
        """
        select id, agent_name, action_summary, action_payload, status, decided_by,
               notification_id, created_at, resolved_at, expires_at
        from approvals where id = $1
        """,
        approval_id,
    )
    return dict(row) if row else None


async def list_pending_approvals(limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select id, agent_name, action_summary, action_payload, status, created_at, expires_at
        from approvals where status = 'pending'
        order by created_at desc limit $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def execute_on_approval(
    outcome: ApprovalOutcome,
    executor: Executor,
    *,
    agent_name: str = "atlas",
    action_type: str = "action_executed",
    episode_id: UUID | None = None,
    goal_id: UUID | None = None,
) -> Any | None:
    """Run `executor` on the approved payload, or nothing if not approved.

    Returns the executor's result on approval, None otherwise. An executor
    that raises is logged and re-raised: a failed *approved* action is a
    real problem the caller must see, unlike a rejection which is a normal
    outcome.
    """
    if not outcome.approved:
        log.info("execute_on_approval skipped, status=%s", outcome.status)
        return None

    payload = outcome.payload or {}
    try:
        result = await executor(payload)
    except Exception as exc:
        log.exception("approved action failed for %s", agent_name)
        await _log_execution(
            agent_name, action_type, payload,
            {"error": f"{type(exc).__name__}: {exc}"}, episode_id, goal_id,
        )
        raise

    await _log_execution(
        agent_name, action_type, payload, {"result": _safe(result)}, episode_id, goal_id,
    )
    return result


async def _log_execution(
    agent_name: str,
    action_type: str,
    payload: dict[str, Any],
    outputs: dict[str, Any],
    episode_id: UUID | None,
    goal_id: UUID | None,
) -> None:
    """Write one row for an executed (post-approval) action."""
    await db.execute(
        """
        insert into actions
          (agent_name, action_type, inputs, outputs, approved_by, episode_id, goal_id)
        values ($1, $2, $3, $4, 'user', $5, $6)
        """,
        agent_name,
        action_type,
        {"payload": payload},
        outputs,
        episode_id,
        goal_id,
    )


def _safe(value: Any) -> Any:
    """Coerce an executor result into something safe for a jsonb column."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "ApprovalOutcome",
    "Executor",
    "DECISIONS",
    "request_approval",
    "open_approval",
    "decide_approval",
    "expire_overdue_approvals",
    "check_approval",
    "list_pending_approvals",
    "execute_on_approval",
]
