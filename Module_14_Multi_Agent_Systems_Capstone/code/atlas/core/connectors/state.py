"""Atlas -- persisted connector cursors.

Each polling connector keeps a cursor (the last GitHub event id, the
last Canvas due-date map, a Gmail historyId) in `connector_state` so a
restart continues where the previous process stopped instead of
re-reading history. The same row records the last success, the last
error and a failure streak, which the health job and the brief can
read without touching the connector.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core import db

log = logging.getLogger(__name__)


async def get_state(source: str) -> dict[str, Any]:
    """The whole row for a source, or an empty default when none exists."""
    row = await db.fetchrow(
        "select * from connector_state where source = $1", source
    )
    if row is None:
        return {
            "source": source,
            "cursor": {},
            "last_ok_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "updated_at": None,
        }
    return dict(row)


async def get_cursor(source: str) -> dict[str, Any]:
    state = await get_state(source)
    return dict(state.get("cursor") or {})


async def set_cursor(source: str, cursor: dict[str, Any]) -> None:
    """Store a new cursor and mark the source healthy."""
    await db.execute(
        """
        insert into connector_state (source, cursor, last_ok_at, last_error,
                                     consecutive_failures, updated_at)
        values ($1, $2, now(), null, 0, now())
        on conflict (source) do update set
          cursor = excluded.cursor,
          last_ok_at = now(),
          last_error = null,
          consecutive_failures = 0,
          updated_at = now()
        """,
        source,
        cursor,
    )


async def record_ok(source: str) -> None:
    """Mark a source healthy without changing its cursor."""
    await db.execute(
        """
        insert into connector_state (source, last_ok_at, updated_at)
        values ($1, now(), now())
        on conflict (source) do update set
          last_ok_at = now(), last_error = null,
          consecutive_failures = 0, updated_at = now()
        """,
        source,
    )


async def record_failure(source: str, error: str) -> int:
    """Record a failed sync; returns the new consecutive failure count."""
    n = await db.fetchval(
        """
        insert into connector_state (source, last_error, consecutive_failures,
                                     updated_at)
        values ($1, $2, 1, now())
        on conflict (source) do update set
          last_error = excluded.last_error,
          consecutive_failures = connector_state.consecutive_failures + 1,
          updated_at = now()
        returning consecutive_failures
        """,
        source,
        error[:1000],
    )
    return int(n)


async def list_states() -> list[dict[str, Any]]:
    rows = await db.fetch("select * from connector_state order by source")
    return [dict(r) for r in rows]


def stale(state: dict[str, Any], *, max_age_minutes: int) -> bool:
    """True when a source has not synced successfully within the window."""
    last: datetime | None = state.get("last_ok_at")
    if last is None:
        return True
    age = datetime.now(last.tzinfo) - last
    return age.total_seconds() > max_age_minutes * 60


__all__ = [
    "get_state",
    "get_cursor",
    "set_cursor",
    "record_ok",
    "record_failure",
    "list_states",
    "stale",
]
