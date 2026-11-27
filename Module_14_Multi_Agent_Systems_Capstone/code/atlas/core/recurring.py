"""Atlas -- recurring tasks: the mortgage on the 1st, the taxes on the 15th,
the review every Friday.

A rule is a row in task_templates, the COO's table: a name, a cadence
(weekly, monthly, quarterly, annual) and, in metadata, the anchor (a
weekday, a day of the month, a month and a day), a time of day and the
project. Nothing used to turn a template into dated work. Now the Planner
does: materialize() writes each rule's occurrences inside the horizon as
ordinary tasks (source 'recurring', source_ref the rule's id, one per due
date, never twice), so the week shows them and the 9 a.m. reminder fires.
A daily job keeps the horizon filled, and quick_add understands "every
month on the 1st". Only rules the Planner made (metadata.planner) are
materialized; the COO's older templates are left as they were.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db
from core.config import settings

log = logging.getLogger(__name__)

CADENCES = ("weekly", "monthly", "quarterly", "annual")
HORIZON_DAYS = 21
_QUARTER_MONTHS = (1, 4, 7, 10)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAY_WORDS = (r"mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|thursday|"
              r"fri|friday|sat|saturday|sun|sunday")
_MONTH_WORDS = (r"jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
                r"sep|sept|september|oct|october|nov|november|dec|december")
_DOM = r"(?:the\s+)?(\d{1,2}|last)(?:st|nd|rd|th)?(?:\s+day)?"


# ------------------------------------------------------------------ the phrase


def parse_rule(text: str, today: date | None = None) -> tuple[dict[str, Any] | None, str, str]:
    """(rule, the phrase that said it, the text without it). No rule: (None, "", text)."""
    today = today or date.today()
    s = text

    def hit(pattern: str) -> re.Match[str] | None:
        return re.search(pattern, s, re.I)

    # every month on the 1st / monthly on the 15th / every 1st
    m = hit(rf"\b(?:(?:every|each)\s+month|monthly)(?:\s+on\s+{_DOM})?\b") or hit(rf"\b(?:every|each)\s+{_DOM}\s+of\s+the\s+month\b")
    if m:
        return {"cadence": "monthly", "day": _dom(m.group(1))}, m.group(0), _cut(s, m)
    m = hit(rf"\b(?:every|each)\s+{_DOM}\b(?!\s+(?:of\s+)?(?:{_MONTH_WORDS}))")
    if m and (m.group(1).lower() == "last" or m.group(0).lower().rstrip()[-2:] in ("st", "nd", "rd", "th")):
        return {"cadence": "monthly", "day": _dom(m.group(1))}, m.group(0), _cut(s, m)
    # every quarter on the 15th / quarterly
    m = hit(rf"\b(?:(?:every|each)\s+quarter|quarterly)(?:\s+on\s+{_DOM})?\b")
    if m:
        return {"cadence": "quarterly", "day": _dom(m.group(1)), "months": list(_QUARTER_MONTHS)}, m.group(0), _cut(s, m)
    # every year on April 15 / annually on Apr 15 / yearly / every April 15
    m = hit(rf"\b(?:(?:every|each)\s+year|annually|yearly)(?:\s+on\s+({_MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?)?\b") \
        or hit(rf"\b(?:every|each)\s+({_MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
    if m:
        month = _MONTHS.index(m.group(1)[:3].lower()) + 1 if m.group(1) else today.month
        day = int(m.group(2)) if m.group(2) else today.day
        return {"cadence": "annual", "month": month, "day": day}, m.group(0), _cut(s, m)
    # every friday / weekly on friday / every week
    m = hit(rf"\b(?:(?:every|each)\s+({_DAY_WORDS})s?|weekly(?:\s+on\s+({_DAY_WORDS}))?|(?:every|each)\s+week)\b")
    if m:
        word = m.group(1) or m.group(2)
        weekday = _DAYS.index(word[:3].lower()) if word else today.weekday()
        return {"cadence": "weekly", "weekday": weekday}, m.group(0), _cut(s, m)
    return None, "", text


def _dom(raw: str | None) -> int:
    if not raw:
        return 1
    if raw.lower() == "last":
        return -1
    return max(1, min(31, int(raw)))


def _cut(s: str, m: re.Match[str]) -> str:
    return re.sub(r"\s+", " ", s[: m.start()] + " " + s[m.end():]).strip()


def describe(rule: dict[str, Any]) -> str:
    cadence = rule.get("cadence")
    if cadence == "weekly":
        return f"every {_DAY_NAMES[int(rule.get('weekday', 0)) % 7]}"
    if cadence == "monthly":
        return f"every month on the {_ordinal(rule.get('day', 1))}"
    if cadence == "quarterly":
        return f"every quarter on the {_ordinal(rule.get('day', 1))}"
    if cadence == "annual":
        return f"every year on {calendar.month_name[int(rule.get('month', 1))]} {int(rule.get('day', 1))}"
    return str(cadence or "")


def _ordinal(day: Any) -> str:
    d = int(day)
    if d == -1:
        return "last day"
    suffix = "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return f"{d}{suffix}"


# ------------------------------------------------------------------ the dates


def _clamp(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if day == -1 else min(day, last))


def occurrences(rule: dict[str, Any], start: date, end: date) -> list[date]:
    """Every due date of the rule from start to end, inclusive."""
    if end < start:
        return []
    cadence = rule.get("cadence")
    out: list[date] = []
    if cadence == "weekly":
        wd = int(rule.get("weekday", 0)) % 7
        d = start + timedelta(days=(wd - start.weekday()) % 7)
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
        return out
    day = int(rule.get("day", 1))
    if cadence == "monthly":
        months = list(range(1, 13))
    elif cadence == "quarterly":
        months = [int(m) for m in rule.get("months") or _QUARTER_MONTHS]
    elif cadence == "annual":
        months = [int(rule.get("month", 1))]
    else:
        return []
    y, mo = start.year, start.month
    while date(y, mo, 1) <= end:
        if mo in months:
            d = _clamp(y, mo, day)
            if start <= d <= end:
                out.append(d)
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return out


def next_due(rule: dict[str, Any], after: date) -> date | None:
    found = occurrences(rule, after + timedelta(days=1), after + timedelta(days=400))
    return found[0] if found else None


# ------------------------------------------------------------------ the rows


async def add_rule(title: str, rule: dict[str, Any], *, project: dict[str, Any] | None = None,
                   source: str = "owner", at: time | None = None) -> dict[str, Any]:
    """A rule of the Planner's own, in the COO's task_templates table."""
    if rule.get("cadence") not in CADENCES:
        raise ValueError(f"cadence must be one of {CADENCES}")
    meta = {**rule, "planner": True, "source": source,
            "project_id": str(project["id"]) if project else None,
            "time": at.isoformat(timespec="minutes") if at else None}
    row = await db.fetchrow(
        "insert into task_templates (name, cadence, domain, metadata) values ($1, $2, $3, $4) returning id",
        title, rule["cadence"], project["kind"] if project else None, meta,
    )
    return await get_rule(row["id"])


_RULE_COLS = ("t.id, t.name, t.cadence, t.status, t.last_run, t.created_at, t.metadata, "
              "p.id as project_id, p.slug as project_slug, p.name as project_name, p.kind as project_kind")
_RULE_FROM = ("from task_templates t left join projects p on p.id::text = t.metadata->>'project_id' "
              "where (t.metadata->>'planner')::bool is true")


def _rule(row: Any, today: date) -> dict[str, Any]:
    r = dict(row)
    meta = r.get("metadata") or {}
    project = ({"id": r["project_id"], "slug": r["project_slug"], "name": r["project_name"], "kind": r["project_kind"]}
               if r.get("project_id") else None)
    return {"id": r["id"], "title": r["name"], "cadence": r["cadence"], "status": r["status"],
            "rule": {k: v for k, v in meta.items() if k in ("cadence", "day", "weekday", "month", "months")},
            "describe": describe(meta), "time": meta.get("time"), "next": next_due(meta, today - timedelta(days=1)),
            "project": project, "created_at": r["created_at"]}


async def get_rule(rule_id: UUID, today: date | None = None) -> dict[str, Any]:
    row = await db.fetchrow(f"select {_RULE_COLS} {_RULE_FROM} and t.id = $1", rule_id)
    if row is None:
        raise LookupError(f"no rule {rule_id}")
    return _rule(row, today or _today())


async def list_rules(today: date | None = None) -> list[dict[str, Any]]:
    rows = await db.fetch(f"select {_RULE_COLS} {_RULE_FROM} and t.status = 'active' order by t.created_at")
    today = today or _today()
    out = [_rule(r, today) for r in rows]
    out.sort(key=lambda r: (r["next"] is None, r["next"] or date.max, r["title"].lower()))
    return out


async def stop_rule(rule_id: UUID) -> dict[str, Any] | None:
    """Pause the rule and drop its tasks that are not due yet."""
    res = await db.execute("update task_templates set status = 'paused' where id = $1 and status = 'active' "
                           "and (metadata->>'planner')::bool is true", rule_id)
    if not res.endswith("1"):
        return None
    await db.execute(
        "update deadlines set status = 'cancelled', completed_at = now() "
        "where source = 'recurring' and source_ref = $1 and status = 'open' and due_date > $2",
        str(rule_id), _today(),
    )
    return await get_rule(rule_id)


async def materialize(*, today: date | None = None, horizon_days: int = HORIZON_DAYS,
                      rule_id: UUID | None = None) -> list[dict[str, Any]]:
    """Write the occurrences inside the horizon as tasks, once each.
    Returns the tasks written this time, soonest first."""
    from core import planner

    today = today or _today()
    end = today + timedelta(days=horizon_days)
    args: list[Any] = []
    where = "and t.status = 'active'"
    if rule_id is not None:
        args.append(rule_id)
        where += " and t.id = $1"
    rows = await db.fetch(f"select {_RULE_COLS} {_RULE_FROM} {where} order by t.created_at", *args)
    written: list[dict[str, Any]] = []
    for r in rows:
        meta = r["metadata"] or {}
        pid = UUID(meta["project_id"]) if meta.get("project_id") else None
        at = time.fromisoformat(meta["time"]) if meta.get("time") else None
        for due in occurrences(meta, today, end):
            exists = await db.fetchval(
                "select 1 from deadlines where source = 'recurring' and source_ref = $1 and due_date = $2",
                str(r["id"]), due,
            )
            if exists:
                continue
            due_at = datetime.combine(due, at, tzinfo=ZoneInfo(settings.timezone)) if at else None
            task_id = await db.fetchval(
                """
                insert into deadlines (title, due_date, due_at, priority, source, source_ref, project_id, domain)
                values ($1, $2, $3, 'normal', 'recurring', $4, $5, $6) returning id
                """,
                r["name"], due, due_at, str(r["id"]), pid, r["project_kind"],
            )
            await planner._remind(task_id, r["name"], due)
            task = await planner.get_task(task_id)
            if task:
                written.append(task)
        await db.execute("update task_templates set last_run = greatest(coalesce(last_run, $2), $2) where id = $1",
                         r["id"], end)
    written.sort(key=lambda t: (t["when"] or date.max, t["title"]))
    if written:
        log.info("recurring: wrote %d task(s) up to %s", len(written), end.isoformat())
    return written


async def scheduled_materialize() -> None:
    """Scheduler entry, daily. Never raises."""
    try:
        await materialize()
    except Exception:
        log.exception("recurring: could not materialize the rules")


def _today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


__all__ = ["CADENCES", "HORIZON_DAYS", "parse_rule", "describe", "occurrences", "next_due", "add_rule", "get_rule",
           "list_rules", "stop_rule", "materialize", "scheduled_materialize"]
