"""Atlas -- goal management.

A thin data-access layer over the goals table. Goals are what the CEO
tracks and, from the Week 2 executive loop onward, scans the world
against. A goal is one row: a description, an owner (the agent that
carries it), a status (active, paused, closed, abandoned), and optional
success_criteria and target_date. close_goal stamps closed_at.

This module is plain CRUD. The CEO reaches it through its goal tools;
the /goal Telegram command reaches it directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from core import db

# Columns selected for every goal read, in one place so the row->dict
# shape stays consistent across functions.
_COLS = (
    "id, description, owner, status, success_criteria, "
    "created_at, target_date, closed_at, metadata"
)

# Statuses a goal can be moved into by close_goal. 'active' is the
# creation default and is not a close target.
_CLOSE_STATUSES = ("paused", "closed", "abandoned")


async def create_goal(
    description: str,
    owner: str = "ceo",
    success_criteria: str | None = None,
    target_date: date | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Insert an active goal, return its id."""
    row = await db.fetchrow(
        """
        insert into goals
          (description, owner, success_criteria, target_date, metadata)
        values ($1, $2, $3, $4, $5)
        returning id
        """,
        description,
        owner,
        success_criteria,
        target_date,
        metadata or {},
    )
    return row["id"]


async def list_goals(status: str | None = "active") -> list[dict[str, Any]]:
    """List goals, newest first. status=None returns every goal."""
    if status is None:
        rows = await db.fetch(
            f"select {_COLS} from goals order by created_at desc"
        )
    else:
        rows = await db.fetch(
            f"select {_COLS} from goals where status = $1 "
            "order by created_at desc",
            status,
        )
    return [dict(r) for r in rows]


async def get_goal(goal_id: UUID) -> dict[str, Any] | None:
    """Return one goal by id, or None."""
    row = await db.fetchrow(
        f"select {_COLS} from goals where id = $1", goal_id
    )
    return dict(row) if row else None


async def find_goal(raw: str) -> dict[str, Any] | None:
    """Resolve a goal by full UUID or an 8+ character id prefix.

    Returns None when there is no match, the prefix is too short, or the
    prefix is ambiguous (matches more than one goal).
    """
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None

    if uid is not None:
        return await get_goal(uid)

    if len(raw) < 8:
        return None

    rows = await db.fetch(
        f"select {_COLS} from goals where id::text like $1 limit 2",
        f"{raw}%",
    )
    if len(rows) != 1:
        return None
    return dict(rows[0])


async def close_goal(goal_id: UUID, status: str = "closed") -> bool:
    """Move a goal to a non-active status. Returns True if a row changed.

    status must be one of paused, closed, abandoned. closed_at is stamped
    for the terminal states (closed, abandoned) and left null for paused
    so a paused goal can later be reactivated cleanly.
    """
    if status not in _CLOSE_STATUSES:
        raise ValueError(
            f"invalid close status {status!r}; "
            f"expected one of {_CLOSE_STATUSES}"
        )
    result = await db.execute(
        """
        update goals
        set status = $2,
            closed_at = case when $2 = 'paused' then null else now() end
        where id = $1
        """,
        goal_id,
        status,
    )
    return not result.endswith(" 0")


__all__ = [
    "create_goal",
    "list_goals",
    "get_goal",
    "find_goal",
    "close_goal",
]
