"""Atlas -- COO operations data layer.

The store behind the COO agent: dated deadlines the COO tracks to
completion, and the recurring task templates it knows to expect. It sits
on top of migration 004.

Deadlines wire into the executive loop for free. When a deadline is
created with a reminder, a prospective trigger is created alongside it
with a fires_at before the due date. The executive loop already polls
due triggers every tick, so the reminder surfaces with no change to the
loop. Completing or cancelling a deadline cancels its pending trigger,
so a reminder never fires for work already done.

This module is plain data access. The judgment, whether a deadline is
genuinely slipping and what to do about it, lives in the COO agent. The
one shared piece of arithmetic, classifying a deadline by how close it
is, is a pure helper so it can be tested without a database.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db
from core.config import settings
from core.memory import cancel_trigger, create_trigger

_DEADLINE_COLS = (
    "id, title, due_date, domain, priority, status, notes, source, "
    "trigger_id, created_at, completed_at, metadata"
)
_TEMPLATE_COLS = (
    "id, name, cadence, domain, lead_time_days, checklist, status, "
    "last_run, created_at, metadata"
)

_DEADLINE_STATUSES = ("open", "done", "missed", "cancelled")
_PRIORITIES = ("low", "normal", "high")
_CADENCES = ("weekly", "monthly", "quarterly", "annual")

# A deadline within this many days is "due soon"; further out is "upcoming".
_DUE_SOON_DAYS = 3

_TEMPLATE_UPDATABLE = (
    "name",
    "cadence",
    "domain",
    "lead_time_days",
    "checklist",
    "status",
    "last_run",
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def classify_deadline(due_date: date, today: date) -> tuple[str, int]:
    """Classify a deadline by how close it is.

    Returns (state, days_until) where state is one of overdue, due_today,
    due_soon, upcoming, and days_until is negative once the date passes.
    """
    days = (due_date - today).days
    if days < 0:
        state = "overdue"
    elif days == 0:
        state = "due_today"
    elif days <= _DUE_SOON_DAYS:
        state = "due_soon"
    else:
        state = "upcoming"
    return state, days


def _reminder_fires_at(due_date: date, days_before: int) -> datetime:
    """When a deadline reminder should fire: 9am local, days_before early."""
    remind_on = due_date - timedelta(days=max(0, days_before))
    return datetime.combine(
        remind_on, time(9, 0), tzinfo=ZoneInfo(settings.timezone)
    )


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


async def add_deadline(
    title: str,
    due_date: date,
    domain: str | None = None,
    priority: str = "normal",
    notes: str | None = None,
    source: str = "owner",
    remind_days_before: int | None = None,
) -> UUID:
    """Create a deadline. If remind_days_before is set, also create a
    prospective trigger so the executive loop reminds Trilok early.

    Returns the deadline id.
    """
    if priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}")
    row = await db.fetchrow(
        """
        insert into deadlines (title, due_date, domain, priority, notes, source)
        values ($1, $2, $3, $4, $5, $6)
        returning id
        """,
        title,
        due_date,
        domain,
        priority,
        notes,
        source,
    )
    deadline_id = row["id"]

    if remind_days_before is not None:
        trigger_id = await create_trigger(
            trigger_type="time",
            spec={
                "kind": "deadline_reminder",
                "deadline_id": str(deadline_id),
            },
            payload={
                "message": f"{title} is due {due_date.isoformat()}."
            },
            description=f"Deadline reminder: {title}",
            fires_at=_reminder_fires_at(due_date, remind_days_before),
        )
        await db.execute(
            "update deadlines set trigger_id = $2 where id = $1",
            deadline_id,
            trigger_id,
        )
    return deadline_id


async def list_deadlines(
    status: str | None = "open",
    domain: str | None = None,
    due_before: date | None = None,
) -> list[dict[str, Any]]:
    """List deadlines, soonest due first. status=None returns every one."""
    clauses: list[str] = []
    args: list[Any] = []
    if status is not None:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    if domain:
        args.append(domain)
        clauses.append(f"domain = ${len(args)}")
    if due_before is not None:
        args.append(due_before)
        clauses.append(f"due_date <= ${len(args)}")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = await db.fetch(
        f"""
        select {_DEADLINE_COLS} from deadlines {where}
        order by due_date,
          case priority when 'high' then 0 when 'normal' then 1 else 2 end
        """,
        *args,
    )
    return [dict(r) for r in rows]


async def find_deadline(raw: str) -> dict[str, Any] | None:
    """Resolve a deadline by full UUID or an 8+ character id prefix."""
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None
    if uid is not None:
        row = await db.fetchrow(
            f"select {_DEADLINE_COLS} from deadlines where id = $1", uid
        )
        return dict(row) if row else None
    if len(raw) < 8:
        return None
    rows = await db.fetch(
        f"select {_DEADLINE_COLS} from deadlines where id::text like $1 "
        "limit 2",
        f"{raw}%",
    )
    return dict(rows[0]) if len(rows) == 1 else None


async def complete_deadline(
    deadline_id: UUID, status: str = "done"
) -> bool:
    """Close a deadline (done, missed, or cancelled) and cancel its
    reminder trigger if it has one. Returns True if a row changed."""
    if status not in ("done", "missed", "cancelled"):
        raise ValueError("status must be done, missed, or cancelled")
    row = await db.fetchrow(
        "select trigger_id from deadlines where id = $1", deadline_id
    )
    if row is None:
        return False
    await db.execute(
        """
        update deadlines
        set status = $2, completed_at = now()
        where id = $1
        """,
        deadline_id,
        status,
    )
    if row["trigger_id"] is not None:
        await cancel_trigger(row["trigger_id"])
    return True


async def deadlines_needing_attention(
    horizon_days: int = 7,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Open deadlines that are overdue or due within horizon_days.

    This is the "is this slipping?" check. Each row gets a `state` and a
    `days_until` from classify_deadline so the COO can speak to it.
    """
    today = today or date.today()
    cutoff = today + timedelta(days=max(0, horizon_days))
    rows = await db.fetch(
        f"""
        select {_DEADLINE_COLS} from deadlines
        where status = 'open' and due_date <= $1
        order by due_date,
          case priority when 'high' then 0 when 'normal' then 1 else 2 end
        """,
        cutoff,
    )
    result = []
    for r in rows:
        item = dict(r)
        state, days = classify_deadline(r["due_date"], today)
        item["state"] = state
        item["days_until"] = days
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Task templates
# ---------------------------------------------------------------------------


async def add_task_template(
    name: str,
    cadence: str,
    domain: str | None = None,
    lead_time_days: int = 0,
    checklist: str | None = None,
) -> UUID:
    """Create a recurring task template. Returns its id."""
    if cadence not in _CADENCES:
        raise ValueError(f"cadence must be one of {_CADENCES}")
    row = await db.fetchrow(
        """
        insert into task_templates
          (name, cadence, domain, lead_time_days, checklist)
        values ($1, $2, $3, $4, $5)
        returning id
        """,
        name,
        cadence,
        domain,
        max(0, lead_time_days),
        checklist,
    )
    return row["id"]


async def list_task_templates(
    status: str | None = "active",
) -> list[dict[str, Any]]:
    """List task templates. status=None returns every one."""
    if status is None:
        rows = await db.fetch(
            f"select {_TEMPLATE_COLS} from task_templates order by name"
        )
    else:
        rows = await db.fetch(
            f"select {_TEMPLATE_COLS} from task_templates "
            "where status = $1 order by name",
            status,
        )
    return [dict(r) for r in rows]


async def find_task_template(raw: str) -> dict[str, Any] | None:
    """Resolve a template by full UUID or an 8+ character id prefix."""
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None
    if uid is not None:
        row = await db.fetchrow(
            f"select {_TEMPLATE_COLS} from task_templates where id = $1", uid
        )
        return dict(row) if row else None
    if len(raw) < 8:
        return None
    rows = await db.fetch(
        f"select {_TEMPLATE_COLS} from task_templates "
        "where id::text like $1 limit 2",
        f"{raw}%",
    )
    return dict(rows[0]) if len(rows) == 1 else None


async def update_task_template(template_id: UUID, **fields: Any) -> bool:
    """Update a template's mutable fields. Returns True if a row changed."""
    sets = {k: v for k, v in fields.items() if k in _TEMPLATE_UPDATABLE}
    if not sets:
        raise ValueError(
            f"nothing to update; allowed fields: {_TEMPLATE_UPDATABLE}"
        )
    if "cadence" in sets and sets["cadence"] not in _CADENCES:
        raise ValueError(f"cadence must be one of {_CADENCES}")
    assignments = []
    args: list[Any] = [template_id]
    for col, value in sets.items():
        args.append(value)
        assignments.append(f"{col} = ${len(args)}")
    result = await db.execute(
        f"update task_templates set {', '.join(assignments)} where id = $1",
        *args,
    )
    return not result.endswith(" 0")


__all__ = [
    "classify_deadline",
    "add_deadline",
    "list_deadlines",
    "find_deadline",
    "complete_deadline",
    "deadlines_needing_attention",
    "add_task_template",
    "list_task_templates",
    "find_task_template",
    "update_task_template",
]
