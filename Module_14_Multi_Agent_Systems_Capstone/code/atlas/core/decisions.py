"""Atlas -- the decision journal.

Every real call, Trilok's or an agent's, is recorded here with its
rationale and the outcome it expects. Later the decision is revisited and
the outcome assessed, so Atlas can say plainly whether a past call worked.
Over time this is what makes Trilok a better decision-maker, not just a
more organized one.

Plain CRUD over the decisions table (migration 002).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from core import db

_COLS = (
    "id, summary, rationale, expected_outcome, made_by, domain, status, "
    "outcome, assessment, decided_at, review_at, resolved_at, goal_id, "
    "episode_id, metadata"
)

# How a resolved decision turned out, in hindsight.
_ASSESSMENTS = ("right", "wrong", "mixed", "unclear")


async def log_decision(
    summary: str,
    rationale: str | None = None,
    expected_outcome: str | None = None,
    made_by: str = "owner",
    domain: str | None = None,
    review_at: date | None = None,
    goal_id: UUID | None = None,
    episode_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Record one decision. Returns its id."""
    row = await db.fetchrow(
        """
        insert into decisions
          (summary, rationale, expected_outcome, made_by, domain,
           review_at, goal_id, episode_id, metadata)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        returning id
        """,
        summary,
        rationale,
        expected_outcome,
        made_by,
        domain,
        review_at,
        goal_id,
        episode_id,
        metadata or {},
    )
    return row["id"]


async def list_decisions(
    status: str | None = "open",
) -> list[dict[str, Any]]:
    """List decisions, newest first. status=None returns every decision."""
    if status is None:
        rows = await db.fetch(
            f"select {_COLS} from decisions order by decided_at desc"
        )
    else:
        rows = await db.fetch(
            f"select {_COLS} from decisions where status = $1 "
            "order by decided_at desc",
            status,
        )
    return [dict(r) for r in rows]


async def get_decision(decision_id: UUID) -> dict[str, Any] | None:
    """Return one decision by id, or None."""
    row = await db.fetchrow(
        f"select {_COLS} from decisions where id = $1", decision_id
    )
    return dict(row) if row else None


async def find_decision(raw: str) -> dict[str, Any] | None:
    """Resolve a decision by full UUID or an 8+ character id prefix."""
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None

    if uid is not None:
        return await get_decision(uid)

    if len(raw) < 8:
        return None

    rows = await db.fetch(
        f"select {_COLS} from decisions where id::text like $1 limit 2",
        f"{raw}%",
    )
    if len(rows) != 1:
        return None
    return dict(rows[0])


async def resolve_decision(
    decision_id: UUID, outcome: str, assessment: str
) -> bool:
    """Close a decision with what happened and how it turned out.

    assessment must be one of right, wrong, mixed, unclear. Returns True
    if a row changed.
    """
    if assessment not in _ASSESSMENTS:
        raise ValueError(
            f"invalid assessment {assessment!r}; "
            f"expected one of {_ASSESSMENTS}"
        )
    result = await db.execute(
        """
        update decisions
        set status = 'resolved',
            outcome = $2,
            assessment = $3,
            resolved_at = now()
        where id = $1
        """,
        decision_id,
        outcome,
        assessment,
    )
    return not result.endswith(" 0")


async def decisions_due_for_review() -> list[dict[str, Any]]:
    """Open decisions whose review_at date has arrived."""
    rows = await db.fetch(
        f"""
        select {_COLS} from decisions
        where status = 'open'
          and review_at is not null
          and review_at <= current_date
        order by review_at
        """
    )
    return [dict(r) for r in rows]


__all__ = [
    "log_decision",
    "list_decisions",
    "get_decision",
    "find_decision",
    "resolve_decision",
    "decisions_due_for_review",
]
