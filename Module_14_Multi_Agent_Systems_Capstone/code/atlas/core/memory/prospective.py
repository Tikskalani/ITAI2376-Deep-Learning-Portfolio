"""Atlas — prospective memory.

Time and event triggers. The executive loop polls get_due_triggers on
every tick and fires anything whose fires_at has passed. Event-driven and
condition-driven triggers will share this table later; for Phase 0 the
common case is time triggers with a concrete fires_at.

mark_fired bumps fire_count and flips status to 'fired' for one-shot
triggers. Recurring triggers (e.g. cron) can be re-armed by the scheduler
by writing a new fires_at; that policy lives in the scheduler, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from core import db


async def create_trigger(
    trigger_type: str,
    spec: dict[str, Any],
    payload: dict[str, Any],
    description: str,
    fires_at: datetime | None = None,
) -> UUID:
    """Create an active trigger. Returns its id."""
    row = await db.fetchrow(
        """
        insert into triggers (trigger_type, spec, payload, description, fires_at, status)
        values ($1, $2, $3, $4, $5, 'active')
        returning id
        """,
        trigger_type,
        spec,
        payload,
        description,
        fires_at,
    )
    return row["id"]


async def get_due_triggers() -> list[dict[str, Any]]:
    """Return active triggers whose fires_at has passed (or is null)."""
    rows = await db.fetch(
        """
        select id, trigger_type, spec, payload, status, description,
               created_at, fires_at, fired_at, fire_count
        from triggers
        where status = 'active'
          and (fires_at is null or fires_at <= now())
        order by coalesce(fires_at, created_at)
        """
    )
    return [
        {
            "id": r["id"],
            "trigger_type": r["trigger_type"],
            "spec": r["spec"],
            "payload": r["payload"],
            "status": r["status"],
            "description": r["description"],
            "created_at": r["created_at"],
            "fires_at": r["fires_at"],
            "fired_at": r["fired_at"],
            "fire_count": r["fire_count"],
        }
        for r in rows
    ]


async def mark_fired(trigger_id: UUID) -> None:
    """Mark a trigger as fired. Increments fire_count and stamps fired_at."""
    await db.execute(
        """
        update triggers
        set status = 'fired',
            fired_at = now(),
            fire_count = fire_count + 1
        where id = $1
        """,
        trigger_id,
    )


async def cancel_trigger(trigger_id: UUID) -> None:
    """Cancel an active trigger. Idempotent — a no-op if already cancelled."""
    await db.execute(
        """
        update triggers
        set status = 'cancelled'
        where id = $1 and status = 'active'
        """,
        trigger_id,
    )
