"""Atlas -- system heartbeats.

A tiny helper layer over the system_heartbeats table (migration 005).
The executive loop and the scheduled brief jobs upsert a row each time
they complete a cycle; the health alerter reads those rows and notices
when something has gone quiet.

The table is upsert-keyed by `kind`, so per-job state stays one row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core import db


async def set_heartbeat(
    kind: str, metadata: dict[str, Any] | None = None
) -> None:
    """Record that `kind` just ticked. Upserts a single row keyed by kind."""
    await db.execute(
        """
        insert into system_heartbeats (kind, last_at, metadata)
        values ($1, now(), $2)
        on conflict (kind) do update
        set last_at = excluded.last_at,
            metadata = excluded.metadata
        """,
        kind,
        metadata or {},
    )


async def get_heartbeat(kind: str) -> datetime | None:
    """Return the most recent timestamp for `kind`, or None if never set."""
    row = await db.fetchrow(
        "select last_at from system_heartbeats where kind = $1", kind
    )
    return row["last_at"] if row else None


async def all_heartbeats() -> dict[str, datetime]:
    """Return every heartbeat, keyed by kind."""
    rows = await db.fetch(
        "select kind, last_at from system_heartbeats"
    )
    return {r["kind"]: r["last_at"] for r in rows}


__all__ = ["set_heartbeat", "get_heartbeat", "all_heartbeats"]
