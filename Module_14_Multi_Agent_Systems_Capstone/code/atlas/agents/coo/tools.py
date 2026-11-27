"""Atlas -- COO agent tools.

The COO's toolset covers two areas:
  * deadlines      -- add_deadline, list_deadlines, complete_deadline,
                      check_slipping
  * task templates -- add_task_template, list_task_templates,
                      update_task_template
plus the shared memory tools from core.runtime.common_tools.

build_tools() returns the ToolSpec list the runtime hands to the model.
The dispatcher calls it through the COO's registration (see agents/coo
register_coo).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.config import settings
from core.connectors.base import ConnectorError
from core.connectors.calendar import CalendarConnector
from core.operations import (
    add_deadline,
    add_task_template,
    complete_deadline,
    deadlines_needing_attention,
    find_deadline,
    find_task_template,
    list_deadlines,
    list_task_templates,
    update_task_template,
)
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    remember_fact_tool,
)

_PRIORITIES = ("low", "normal", "high")
_CADENCES = ("weekly", "monthly", "quarterly", "annual")
_CLOSE_STATUSES = ("done", "missed", "cancelled")


def _parse_iso_date(raw: Any) -> tuple[date | None, str | None]:
    """Parse an ISO date argument. Returns (date, error_message)."""
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


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def _deadline_view(row: dict[str, Any]) -> dict[str, Any]:
    """Trim a deadline row to the fields worth showing the model."""
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


async def _add_deadline(args: dict[str, Any]) -> dict[str, Any]:
    """Record a dated commitment, optionally with a reminder."""
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title is required"}
    due_date, err = _parse_iso_date(args.get("due_date"))
    if err:
        return {"error": f"due_date {err}"}
    if due_date is None:
        return {"error": "due_date is required"}

    priority = (args.get("priority") or "normal").strip().lower()
    if priority not in _PRIORITIES:
        return {"error": f"priority must be one of {_PRIORITIES}"}

    remind_raw = args.get("remind_days_before")
    remind: int | None = None
    if remind_raw not in (None, ""):
        try:
            remind = max(0, int(remind_raw))
        except (TypeError, ValueError):
            return {"error": f"remind_days_before must be a number, got {remind_raw!r}"}

    deadline_id = await add_deadline(
        title=title,
        due_date=due_date,
        domain=(args.get("domain") or "").strip() or None,
        priority=priority,
        notes=(args.get("notes") or "").strip() or None,
        remind_days_before=remind,
    )
    return {
        "deadline_id": str(deadline_id),
        "title": title,
        "due_date": due_date.isoformat(),
        "reminder_set": remind is not None,
    }


async def _list_deadlines(args: dict[str, Any]) -> dict[str, Any]:
    """List tracked deadlines, open ones by default."""
    status = (args.get("status") or "open").strip().lower()
    domain = (args.get("domain") or "").strip() or None
    rows = await list_deadlines(
        status=None if status == "all" else status, domain=domain
    )
    return {
        "status_filter": status,
        "domain_filter": domain,
        "count": len(rows),
        "deadlines": [_deadline_view(r) for r in rows],
    }


async def _complete_deadline(args: dict[str, Any]) -> dict[str, Any]:
    """Close a deadline as done, missed, or cancelled."""
    raw = (args.get("deadline_id") or "").strip()
    if not raw:
        return {"error": "deadline_id is required"}
    deadline = await find_deadline(raw)
    if deadline is None:
        return {"error": f"no deadline matches {raw!r}"}

    status = (args.get("status") or "done").strip().lower()
    if status not in _CLOSE_STATUSES:
        return {"error": f"status must be one of {_CLOSE_STATUSES}"}
    changed = await complete_deadline(deadline["id"], status=status)
    return {
        "deadline_id": str(deadline["id"]),
        "title": deadline["title"],
        "status": status,
        "changed": changed,
    }


async def _check_slipping(args: dict[str, Any]) -> dict[str, Any]:
    """Return open deadlines that are overdue or due within the horizon."""
    horizon = _clamp_int(args.get("horizon_days"), default=7, low=1, high=90)
    rows = await deadlines_needing_attention(horizon_days=horizon)
    overdue = [r for r in rows if r["state"] == "overdue"]
    due_now = [r for r in rows if r["state"] == "due_today"]
    due_soon = [r for r in rows if r["state"] in ("due_soon", "upcoming")]
    return {
        "horizon_days": horizon,
        "overdue_count": len(overdue),
        "overdue": [_deadline_view(r) for r in overdue],
        "due_today": [_deadline_view(r) for r in due_now],
        "due_soon": [_deadline_view(r) for r in due_soon],
    }


# ---------------------------------------------------------------------------
# Task templates
# ---------------------------------------------------------------------------


def _template_view(row: dict[str, Any]) -> dict[str, Any]:
    """Trim a task template row to the fields worth showing the model."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "cadence": row["cadence"],
        "domain": row["domain"],
        "lead_time_days": row["lead_time_days"],
        "checklist": row["checklist"],
        "status": row["status"],
        "last_run": (
            row["last_run"].isoformat() if row["last_run"] else None
        ),
    }


async def _add_task_template(args: dict[str, Any]) -> dict[str, Any]:
    """Add a recurring task template."""
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    cadence = (args.get("cadence") or "").strip().lower()
    if cadence not in _CADENCES:
        return {"error": f"cadence must be one of {_CADENCES}"}
    lead_time = _clamp_int(
        args.get("lead_time_days"), default=0, low=0, high=180
    )
    template_id = await add_task_template(
        name=name,
        cadence=cadence,
        domain=(args.get("domain") or "").strip() or None,
        lead_time_days=lead_time,
        checklist=(args.get("checklist") or "").strip() or None,
    )
    return {"template_id": str(template_id), "added": name}


async def _list_task_templates(args: dict[str, Any]) -> dict[str, Any]:
    """List task templates, active ones by default."""
    status = (args.get("status") or "active").strip().lower()
    rows = await list_task_templates(
        status=None if status == "all" else status
    )
    return {
        "status_filter": status,
        "count": len(rows),
        "templates": [_template_view(r) for r in rows],
    }


async def _update_task_template(args: dict[str, Any]) -> dict[str, Any]:
    """Update a task template's cadence, status, lead time, and so on."""
    raw = (args.get("template_id") or "").strip()
    if not raw:
        return {"error": "template_id is required"}
    template = await find_task_template(raw)
    if template is None:
        return {"error": f"no template matches {raw!r}"}

    fields: dict[str, Any] = {}
    if args.get("name"):
        fields["name"] = str(args["name"]).strip()
    if args.get("cadence"):
        cadence = str(args["cadence"]).strip().lower()
        if cadence not in _CADENCES:
            return {"error": f"cadence must be one of {_CADENCES}"}
        fields["cadence"] = cadence
    if args.get("domain"):
        fields["domain"] = str(args["domain"]).strip()
    if args.get("lead_time_days") not in (None, ""):
        fields["lead_time_days"] = _clamp_int(
            args.get("lead_time_days"), default=0, low=0, high=180
        )
    if args.get("checklist") is not None and str(args.get("checklist")).strip():
        fields["checklist"] = str(args["checklist"]).strip()
    if args.get("status"):
        status = str(args["status"]).strip().lower()
        if status not in ("active", "paused"):
            return {"error": "status must be active or paused"}
        fields["status"] = status
    if args.get("last_run"):
        last_run, err = _parse_iso_date(args.get("last_run"))
        if err:
            return {"error": f"last_run {err}"}
        fields["last_run"] = last_run

    if not fields:
        return {"error": "nothing to update; pass at least one field"}
    changed = await update_task_template(template["id"], **fields)
    return {
        "template_id": str(template["id"]),
        "name": template["name"],
        "changed": changed,
        "updated_fields": sorted(fields),
    }


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


_ADD_DEADLINE = ToolSpec(
    name="add_deadline",
    description=(
        "Record a dated commitment to track. Give a clear title and a due "
        "date (ISO YYYY-MM-DD). Set domain to file it in the right world "
        "(a project slug such as demo-dental or hcc, or property, money, household, "
        "atlas), priority (low, normal, high) when it matters, and "
        "remind_days_before to have Trilok reminded that many days early. "
        "The reminder fires through the executive loop on its own."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What is due."},
            "due_date": {
                "type": "string",
                "description": "Due date, ISO YYYY-MM-DD.",
            },
            "domain": {"type": "string", "description": "Which world it belongs to."},
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
    handler=_add_deadline,
)

_LIST_DEADLINES = ToolSpec(
    name="list_deadlines",
    description=(
        "List tracked deadlines, soonest due first. Defaults to open "
        "deadlines. Pass status (open, done, missed, cancelled, or all) "
        "and an optional domain to filter."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "open, done, missed, cancelled, or all.",
            },
            "domain": {"type": "string", "description": "Domain filter."},
        },
        "required": [],
    },
    handler=_list_deadlines,
)

_COMPLETE_DEADLINE = ToolSpec(
    name="complete_deadline",
    description=(
        "Close a deadline. Use 'done' when it happened, 'missed' when it "
        "did not, 'cancelled' when it stopped mattering. Be honest with "
        "which: a missed deadline marked done teaches Atlas nothing. "
        "Closing a deadline cancels its reminder. The deadline_id may be a "
        "full id or its first 8+ characters."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "deadline_id": {
                "type": "string",
                "description": "Deadline id, or its first 8+ characters.",
            },
            "status": {
                "type": "string",
                "description": "done, missed, or cancelled. Default done.",
            },
        },
        "required": ["deadline_id"],
    },
    handler=_complete_deadline,
)

_CHECK_SLIPPING = ToolSpec(
    name="check_slipping",
    description=(
        "The slipping check: every open deadline that is overdue or due "
        "within the horizon, split into overdue, due today, and due soon, "
        "each marked with how many days out it is. Run this whenever "
        "Trilok asks where things stand. horizon_days defaults to 7."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "horizon_days": {
                "type": "integer",
                "description": "How many days ahead to look, 1-90. Default 7.",
            }
        },
        "required": [],
    },
    handler=_check_slipping,
)

_ADD_TASK_TEMPLATE = ToolSpec(
    name="add_task_template",
    description=(
        "Add a recurring task template: the monthly statement "
        "reconciliation, the quarterly tax estimate, the annual ACA "
        "renewal. Record the cadence (weekly, monthly, quarterly, annual) "
        "and lead_time_days, how much runway the work needs. When a "
        "template comes due, turn it into a deadline with add_deadline."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The recurring task."},
            "cadence": {
                "type": "string",
                "description": "weekly, monthly, quarterly, or annual.",
            },
            "domain": {"type": "string", "description": "Which world it belongs to."},
            "lead_time_days": {
                "type": "integer",
                "description": "Days of runway the work needs. Default 0.",
            },
            "checklist": {
                "type": "string",
                "description": "What the task involves. Optional.",
            },
        },
        "required": ["name", "cadence"],
    },
    handler=_add_task_template,
)

_LIST_TASK_TEMPLATES = ToolSpec(
    name="list_task_templates",
    description=(
        "List recurring task templates, active ones by default. Pass "
        "status (active, paused, or all) to filter."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "active, paused, or all. Default active.",
            }
        },
        "required": [],
    },
    handler=_list_task_templates,
)

_UPDATE_TASK_TEMPLATE = ToolSpec(
    name="update_task_template",
    description=(
        "Update a task template: change its name, cadence, domain, lead "
        "time, checklist, or status (active, paused), or set last_run to "
        "the date it was last turned into a deadline. The template_id may "
        "be a full id or its first 8+ characters."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "template_id": {
                "type": "string",
                "description": "Template id, or its first 8+ characters.",
            },
            "name": {"type": "string", "description": "New name."},
            "cadence": {
                "type": "string",
                "description": "weekly, monthly, quarterly, or annual.",
            },
            "domain": {"type": "string", "description": "New domain."},
            "lead_time_days": {
                "type": "integer",
                "description": "New lead time in days.",
            },
            "checklist": {"type": "string", "description": "Updated checklist."},
            "status": {
                "type": "string",
                "description": "active or paused.",
            },
            "last_run": {
                "type": "string",
                "description": "ISO date the template was last instantiated.",
            },
        },
        "required": ["template_id"],
    },
    handler=_update_task_template,
)


# A single calendar connector instance is reused across dispatches so
# the in-memory ICS cache survives. Rebuilt only if settings change
# under our feet, which they shouldn't in a running process.
_CALENDAR_CONN: CalendarConnector | None = None


def _calendar_connector() -> CalendarConnector | None:
    """Return the shared calendar connector, or None if not configured."""
    global _CALENDAR_CONN
    url = settings.calendar_ics_url
    if not url:
        return None
    if _CALENDAR_CONN is None or _CALENDAR_CONN._url != url:  # noqa: SLF001
        _CALENDAR_CONN = CalendarConnector(url, default_tz=settings.timezone)
    return _CALENDAR_CONN


async def _upcoming_events(args: dict[str, Any]) -> dict[str, Any]:
    """Read upcoming calendar events from the configured Google Calendar
    ICS feed. Recurring events are deliberately skipped (per the saved
    design); only one-off events appear in the result.
    """
    days = _clamp_int(args.get("days_ahead"), default=14, low=1, high=180)

    # The Calendar API, when Google is connected: recurring events expand,
    # changes are live. The ICS feed stays as the fallback.
    from core.connectors.google_auth import configured as google_configured

    if google_configured():
        from core.connectors.gcal import GoogleCalendarConnector

        try:
            events = await GoogleCalendarConnector().upcoming(days=days)
        except Exception as exc:
            return {"error": f"could not read calendar: {exc}", "configured": True, "source": "api"}
        return {
            "configured": True,
            "source": "api",
            "days_ahead": days,
            "count": len(events),
            "note": "Recurring events are expanded.",
            "events": events,
        }

    connector = _calendar_connector()
    if connector is None:
        return {
            "error": (
                "calendar not configured. Connect Google (ATLAS_GOOGLE_* in .env, "
                "python -m scripts.google_auth) or set ATLAS_CALENDAR_ICS_URL "
                "to the Google Calendar 'secret address in iCal format' URL."
            ),
            "configured": False,
        }
    try:
        result = await connector.get("all")
    except ConnectorError as exc:
        return {"error": f"could not read calendar: {exc}"}

    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    cutoff = now_local + timedelta(days=days)
    upcoming = [
        e for e in result.data
        if e.start >= now_local and e.start <= cutoff
    ]
    upcoming.sort(key=lambda e: e.start)

    return {
        "configured": True,
        "source": "ics",
        "days_ahead": days,
        "fetched_at": result.fetched_at,
        "stale": result.stale,
        "count": len(upcoming),
        "note": (
            "Recurring events are not expanded in this first cut; "
            "one-off events only."
        ),
        "events": [
            {
                "summary": e.summary,
                "start": e.start.isoformat(),
                "end": e.end.isoformat() if e.end else None,
                "all_day": e.all_day,
                "location": e.location,
            }
            for e in upcoming
        ],
    }


_UPCOMING_EVENTS = ToolSpec(
    name="upcoming_events",
    description=(
        "Read upcoming events from Trilok's Google Calendar over the next "
        "days_ahead days (1-180, default 14). With Google connected, recurring "
        "events are expanded; on the ICS fallback they are not, and the result "
        "says which. If no calendar is configured, returns a clear 'not "
        "configured' error so the agent can say so plainly rather than guess."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days ahead to look (1-180). Default 14.",
            }
        },
        "required": [],
    },
    handler=_upcoming_events,
)


def _parse_when(value: Any) -> datetime | None:
    """An ISO datetime; a naive one is read in the owner's timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.timezone))
    return dt


async def _calendar_create_event(args: dict[str, Any]) -> dict[str, Any]:
    """Put a hold on Trilok's calendar. The approval policy gates this tool
    (its name matches *create_event*), so a card reaches him first."""
    from core.connectors.google_auth import configured as google_configured

    title = (args.get("title") or "").strip()
    start = _parse_when(args.get("start"))
    if not title or start is None:
        return {"error": "title and an ISO start datetime are required"}
    end = _parse_when(args.get("end"))
    if end is None:
        minutes = _clamp_int(args.get("duration_minutes"), default=60, low=5, high=24 * 60)
        end = start + timedelta(minutes=minutes)
    if end <= start:
        return {"error": "end must be after start"}
    if not google_configured():
        return {
            "error": "Google Calendar is not connected (ATLAS_GOOGLE_* blank); tell Trilok "
            "the hold you wanted to place instead.",
            "configured": False,
        }
    from core.connectors.gcal import GoogleCalendarConnector

    attendees = args.get("attendees")
    if isinstance(attendees, str):
        attendees = [a.strip() for a in attendees.split(",") if a.strip()]
    try:
        out = await GoogleCalendarConnector().create_event(
            title,
            start,
            end,
            description=(args.get("description") or "").strip() or None,
            location=(args.get("location") or "").strip() or None,
            attendees=attendees or None,
        )
    except Exception as exc:
        return {"error": f"could not create the event: {exc}"}
    return {"configured": True, **out}


_CALENDAR_CREATE_EVENT = ToolSpec(
    name="calendar_create_event",
    description=(
        "Place a hold on Trilok's Google Calendar. Give a title, an ISO start "
        "(his local time if no offset), and either an ISO end or "
        "duration_minutes (default 60). Optional description, location, "
        "attendees. Trilok approves every hold on a card before it is created; "
        "if he declines, say so and move on."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601 datetime."},
            "end": {"type": "string", "description": "ISO 8601 datetime. Optional."},
            "duration_minutes": {"type": "integer", "description": "Used when end is absent. Default 60."},
            "description": {"type": "string"},
            "location": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses."},
        },
        "required": ["title", "start"],
    },
    handler=_calendar_create_event,
)


def build_tools() -> list[ToolSpec]:
    """Return the COO's current toolset. Called once per dispatch."""
    return [
        _ADD_DEADLINE,
        _LIST_DEADLINES,
        _COMPLETE_DEADLINE,
        _CHECK_SLIPPING,
        _UPCOMING_EVENTS,
        _CALENDAR_CREATE_EVENT,
        _ADD_TASK_TEMPLATE,
        _LIST_TASK_TEMPLATES,
        _UPDATE_TASK_TEMPLATE,
        recall_facts_tool(),
        remember_fact_tool("coo"),
        recall_lessons_tool(),
    ]


__all__ = ["build_tools"]
