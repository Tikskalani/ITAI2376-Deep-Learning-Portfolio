"""Atlas -- Personal Ops agent tools.

A thin household-domain wrapper over the CFO's recurring bills and the
COO's deadlines. The agent does not own a data layer of its own; it
reads from finance and operations with a household filter, and writes
deadlines tagged for the household domain.

build_tools() returns the ToolSpec list the runtime hands to the model.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from core.finance import list_recurring_bills
from core.operations import add_deadline, deadlines_needing_attention
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    remember_fact_tool,
)

# Streams that count as household for our list filter. None = no stream set yet.
_HOUSEHOLD_STREAMS = {"household", "personal", None}

# Deadline domains that count as household, for the slipping/listing view.
_HOUSEHOLD_DOMAINS = {
    "household",
    "personal",
    "personal_ops",
    "auto_insurance",
    "home_insurance",
    "aca",
    "utilities",
}


def _parse_iso_date(raw: Any) -> tuple[date | None, str | None]:
    """Parse an optional ISO date argument. Returns (date, error_message)."""
    if not raw:
        return None, None
    try:
        return date.fromisoformat(str(raw).strip()), None
    except ValueError:
        return None, f"date must be ISO format YYYY-MM-DD, got {raw!r}"


def _clamp_int(raw: Any, default: int, low: int, high: int) -> int:
    """Coerce an integer argument into [low, high]."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _household_bill_view(bill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(bill["id"]),
        "name": bill["name"],
        "amount": bill["amount"],
        "cadence": bill["cadence"],
        "due_day": bill["due_day"],
        "account": bill["account"],
        "stream": bill["stream"],
        "status": bill["status"],
        "last_seen": (
            bill["last_seen"].isoformat() if bill["last_seen"] else None
        ),
        "notes": bill["notes"],
    }


def _household_deadline_view(row: dict[str, Any]) -> dict[str, Any]:
    view = {
        "id": str(row["id"]),
        "title": row["title"],
        "due_date": row["due_date"].isoformat(),
        "domain": row["domain"],
        "priority": row["priority"],
        "status": row["status"],
        "notes": row["notes"],
        "has_reminder": row["trigger_id"] is not None,
    }
    if "state" in row:
        view["state"] = row["state"]
        view["days_until"] = row["days_until"]
    return view


async def _list_household_bills(args: dict[str, Any]) -> dict[str, Any]:
    """List recurring bills tagged household / personal / unstreamed."""
    status = (args.get("status") or "active").strip().lower()
    bills = await list_recurring_bills(
        status=None if status == "all" else status
    )
    filtered = [
        b for b in bills if (b.get("stream") or None) in _HOUSEHOLD_STREAMS
    ]
    return {
        "status_filter": status,
        "count": len(filtered),
        "bills": [_household_bill_view(b) for b in filtered],
    }


async def _household_deadlines(args: dict[str, Any]) -> dict[str, Any]:
    """Open household deadlines, overdue or due within the horizon."""
    horizon = _clamp_int(args.get("horizon_days"), default=14, low=1, high=120)
    rows = await deadlines_needing_attention(horizon_days=horizon)
    rows = [r for r in rows if (r.get("domain") or "").lower() in _HOUSEHOLD_DOMAINS]
    overdue = [r for r in rows if r["state"] == "overdue"]
    due_today = [r for r in rows if r["state"] == "due_today"]
    due_soon = [r for r in rows if r["state"] in ("due_soon", "upcoming")]
    return {
        "horizon_days": horizon,
        "overdue_count": len(overdue),
        "overdue": [_household_deadline_view(r) for r in overdue],
        "due_today": [_household_deadline_view(r) for r in due_today],
        "due_soon": [_household_deadline_view(r) for r in due_soon],
    }


async def _add_household_deadline(args: dict[str, Any]) -> dict[str, Any]:
    """Record a dated household commitment, optionally with a reminder."""
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title is required"}
    due_date, err = _parse_iso_date(args.get("due_date"))
    if err:
        return {"error": f"due_date {err}"}
    if due_date is None:
        return {"error": "due_date is required"}

    priority = (args.get("priority") or "normal").strip().lower()
    if priority not in ("low", "normal", "high"):
        return {"error": "priority must be low, normal, or high"}

    domain = (args.get("domain") or "household").strip() or "household"

    remind_raw = args.get("remind_days_before")
    remind: int | None = None
    if remind_raw not in (None, ""):
        try:
            remind = max(0, int(remind_raw))
        except (TypeError, ValueError):
            return {
                "error": f"remind_days_before must be a number, got {remind_raw!r}"
            }

    deadline_id = await add_deadline(
        title=title,
        due_date=due_date,
        domain=domain,
        priority=priority,
        notes=(args.get("notes") or "").strip() or None,
        source="personal_ops",
        remind_days_before=remind,
    )
    return {
        "deadline_id": str(deadline_id),
        "title": title,
        "domain": domain,
        "due_date": due_date.isoformat(),
        "reminder_set": remind is not None,
    }


_LIST_HOUSEHOLD_BILLS = ToolSpec(
    name="list_household_bills",
    description=(
        "List recurring bills tagged household, personal, or unstreamed "
        "from the CFO's table. Defaults to active bills; pass status "
        "(active, paused, ended, or all) to filter. Use this to see what "
        "is coming, what each usually costs, and when it last fired."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "active, paused, ended, or all. Default active.",
            }
        },
        "required": [],
    },
    handler=_list_household_bills,
)

_HOUSEHOLD_DEADLINES = ToolSpec(
    name="household_deadlines",
    description=(
        "Return open household deadlines overdue or due within the "
        "horizon, with state (overdue / due_today / due_soon / upcoming) "
        "and days_until on each. Lead any reply with what is overdue. "
        "horizon_days defaults to 14, capped at 120."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "horizon_days": {
                "type": "integer",
                "description": "How many days ahead to look (1-120). Default 14.",
            }
        },
        "required": [],
    },
    handler=_household_deadlines,
)

_ADD_HOUSEHOLD_DEADLINE = ToolSpec(
    name="add_household_deadline",
    description=(
        "Record a dated household commitment. domain defaults to "
        "'household' but use something more specific when it helps "
        "(auto_insurance, home_insurance, aca, utilities). "
        "remind_days_before sets a reminder that fires through the "
        "executive loop on its own."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What is due."},
            "due_date": {
                "type": "string",
                "description": "Due date, ISO YYYY-MM-DD.",
            },
            "domain": {
                "type": "string",
                "description": "Household sub-domain. Default 'household'.",
            },
            "priority": {
                "type": "string",
                "description": "low, normal, or high. Default normal.",
            },
            "notes": {"type": "string", "description": "Anything useful."},
            "remind_days_before": {
                "type": "integer",
                "description": "Days before the due date to remind. Optional.",
            },
        },
        "required": ["title", "due_date"],
    },
    handler=_add_household_deadline,
)


def build_tools() -> list[ToolSpec]:
    """Return the Personal Ops agent's toolset."""
    return [
        _LIST_HOUSEHOLD_BILLS,
        _HOUSEHOLD_DEADLINES,
        _ADD_HOUSEHOLD_DEADLINE,
        recall_facts_tool(),
        remember_fact_tool("personal_ops"),
        recall_lessons_tool(),
    ]


__all__ = ["build_tools"]
