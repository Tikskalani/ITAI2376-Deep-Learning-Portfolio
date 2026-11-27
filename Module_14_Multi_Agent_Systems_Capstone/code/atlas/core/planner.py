"""Atlas -- the Planner: everything dated, on one screen.

The deadlines table is the task list. A task is a dated commitment with a
title and, when known, a definition of done, a project and a time of day.
The Planner lays those next to what the senses already know is dated
(Canvas due dates and calendar events arrive as signals with a deadline),
the night shift's tasks, and each project's next action, so a week reads
top to bottom with nothing to assemble by hand.

quick_add() is how tasks get in: one line, written the way he says it.
"Email the lender by Friday #kirkdale", "Ethics essay outline due 10/17",
"Call the roofer tomorrow 9am !!", a recording's "(due 2026-10-14)". The
date, time, project and priority come out of the text; the rest is the
title. A line with no date is a "someday" task and still lands on the
screen. A dated task gets the executive loop's ordinary 9 a.m. reminder
on its day, through the trigger the COO's deadlines already use.

Nothing here sends anything anywhere.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db, recurring
from core.config import settings
from core.memory.prospective import create_trigger
from core.operations import _reminder_fires_at, complete_deadline

log = logging.getLogger(__name__)

PRIORITIES = ("low", "normal", "high")
# What a day can hold, in the order a day reads.
KINDS = ("event", "class", "task", "next", "night")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# Whole words only: "money" is not Monday and "wedding" is not Wednesday.
_DAY_WORDS = (r"mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|thursday|"
              r"fri|friday|sat|saturday|sun|sunday")
_MONTH_WORDS = (r"jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
                r"sep|sept|september|oct|october|nov|november|dec|december")
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10}
_LEAD = r"(?:\b(?:by|on|due|before|until|till|for)\s+)?(?:\bthe\s+)?"
_ORD = r"(?:st|nd|rd|th)?"


@dataclass
class Parsed:
    """What one quick-add line said."""

    title: str
    due_date: date | None = None
    due_time: time | None = None
    priority: str = "normal"
    project_hint: str | None = None
    rule: dict[str, Any] | None = None               # "every month on the 1st": a recurring task, not a one-off
    spans: list[str] = field(default_factory=list)   # the phrases that were read as dates and times


def today_local() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


# ------------------------------------------------------------------ the parser


def parse_when(text: str, today: date | None = None) -> Parsed:
    """Read the date, time, project and priority out of one line.

    Pure. Dates resolve against `today` (the local date by default); a bare
    weekday is the next one after today, "next <weekday>" is that day of
    next week, a month and day with no year is the next such date.
    """
    today = today or today_local()
    out = Parsed(title=text)
    s = " " + (text or "").strip() + " "

    def take(m: re.Match[str]) -> None:
        nonlocal s
        out.spans.append(m.group(0).strip())
        s = s[: m.start()] + " " + s[m.end():]

    # Project: "#kirkdale" anywhere.
    m = re.search(r"#([a-z0-9][a-z0-9_-]*)", s, re.I)
    if m:
        out.project_hint = m.group(1).lower()
        take(m)
    # Priority: "!!", "urgent", "asap", "high priority".
    m = re.search(r"(?:!!+|\b(?:urgent|asap|high priority|top priority)\b)", s, re.I)
    if m:
        out.priority = "high"
        take(m)
    else:
        m = re.search(r"\b(?:low priority|someday|whenever)\b", s, re.I)
        if m:
            out.priority = "low"
            take(m)

    # A rule first: "every month on the 1st" must not be read as "on the 1st".
    rule, phrase, rest = recurring.parse_rule(s, today)
    if rule:
        out.rule = rule
        out.spans.append(phrase.strip())
        s = " " + rest + " "
    else:
        out.due_date = _date_from(s, today, take)
    out.due_time = _time_from(s, take)

    title = re.sub(r"\s+", " ", s).strip()
    # A connector left dangling once its date is gone: "Email the lender by".
    title = re.sub(r"\s+(?:by|on|due|at|before|until|till|for|in)$", "", title, flags=re.I)
    title = title.strip(" ,.;:-")
    out.title = title[:1].upper() + title[1:]
    return out


def _date_from(s: str, today: date, take: Any) -> date | None:
    """First date phrase found, read and removed. None when the line has none."""
    # (due 2026-10-14): the recordings format.
    m = re.search(r"\(\s*due\s+(\d{4})-(\d{2})-(\d{2})\s*\)", s, re.I)
    if m:
        take(m)
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(_LEAD + r"\b(\d{4})-(\d{2})-(\d{2})\b", s, re.I)
    if m:
        take(m)
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(_LEAD + r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s, re.I)
    if m:
        take(m)
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
        return _month_day(int(m.group(1)), int(m.group(2)), year, today)
    m = re.search(_LEAD + rf"\b({_MONTH_WORDS})\.?\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?\b", s, re.I)
    if m:
        take(m)
        return _month_day(_MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)),
                          int(m.group(3)) if m.group(3) else None, today)
    m = re.search(_LEAD + rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_WORDS})\b\.?(?:,?\s+(\d{{4}}))?", s, re.I)
    if m:
        take(m)
        return _month_day(_MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1)),
                          int(m.group(3)) if m.group(3) else None, today)
    m = re.search(r"\b(today|tonight|eod|end of day)\b", s, re.I)
    if m:
        take(m)
        return today
    m = re.search(_LEAD + r"\b(tomorrow|tmrw|tmr)\b", s, re.I)
    if m:
        take(m)
        return today + timedelta(days=1)
    m = re.search(_LEAD + r"\bin\s+(\d+|" + "|".join(_WORDS) + r")\s+(day|week|month)s?\b", s, re.I)
    if m:
        take(m)
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDS[m.group(1).lower()]
        unit = m.group(2).lower()
        if unit == "day":
            return today + timedelta(days=n)
        if unit == "week":
            return today + timedelta(weeks=n)
        return _add_months(today, n)
    m = re.search(_LEAD + r"\b(?:next|this)\s+week\b", s, re.I)
    if m:
        take(m)
        return _next_weekday(today, 0, next_week="next" in m.group(0).lower())
    m = re.search(_LEAD + r"\b(?:end of (?:the )?|eo)(week|month)\b", s, re.I)
    if m:
        take(m)
        if m.group(1).lower() == "week":
            return _next_weekday(today, 4, allow_today=True)
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    m = re.search(_LEAD + r"\bnext\s+month\b", s, re.I)
    if m:
        take(m)
        return _add_months(today.replace(day=1), 1)
    m = re.search(_LEAD + r"\b(?:this\s+)?weekend\b", s, re.I)
    if m:
        take(m)
        return _next_weekday(today, 5, allow_today=True)
    m = re.search(_LEAD + rf"\b(?:(next|this)\s+)?({_DAY_WORDS})\b", s, re.I)
    if m:
        take(m)
        qualifier = (m.group(1) or "").lower()
        wd = _WEEKDAYS.index(m.group(2)[:3].lower())
        return _next_weekday(today, wd, next_week=qualifier == "next", allow_today=qualifier == "this")
    return None


def _time_from(s: str, take: Any) -> time | None:
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", s, re.I)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if 1 <= hour <= 12 and minute < 60:
            take(m)
            if m.group(3).lower().startswith("p") and hour != 12:
                hour += 12
            if m.group(3).lower().startswith("a") and hour == 12:
                hour = 0
            return time(hour, minute)
    m = re.search(r"\b(?:at\s+)(\d{1,2}):(\d{2})\b", s, re.I)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        take(m)
        return time(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(?:at\s+)?(noon|midnight)\b", s, re.I)
    if m:
        take(m)
        return time(12, 0) if m.group(1).lower() == "noon" else time(0, 0)
    return None


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _month_day(month: int, day: int, year: int | None, today: date) -> date | None:
    if year is not None:
        return _safe_date(year, month, day)
    when = _safe_date(today.year, month, day)
    if when is None:
        return None
    # "Oct 3" said on Oct 11 means next year; yesterday is given a day's grace.
    return when if when >= today - timedelta(days=1) else _safe_date(today.year + 1, month, day)


def _next_weekday(today: date, weekday: int, *, next_week: bool = False, allow_today: bool = False) -> date:
    if next_week:
        monday = today + timedelta(days=(7 - today.weekday()))
        return monday + timedelta(days=weekday)
    ahead = (weekday - today.weekday()) % 7
    if ahead == 0 and not allow_today:
        ahead = 7
    return today + timedelta(days=ahead)


def _add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


# ------------------------------------------------------------------ tasks

_TASK_COLS = ("d.id, d.title, d.due_date, d.due_at, d.priority, d.status, d.source, d.source_ref, "
              "d.definition_of_done, d.notes, d.created_at, d.completed_at, d.project_id, "
              "p.slug as project_slug, p.name as project_name, p.kind as project_kind")
_TASK_FROM = "from deadlines d left join projects p on p.id = d.project_id"


async def quick_add(
    text: str,
    *,
    project: str | UUID | None = None,
    source: str = "owner",
    source_ref: str | None = None,
    definition_of_done: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """One line in, one task on the Planner.

    `project` is a slug, a name or an id and beats a #hint in the text; with
    neither, a project whose slug or name the line mentions is linked. A
    recording's action items pass their recording as source_ref, and the
    same line from the same recording is not added twice.
    """
    today = today or today_local()
    parsed = parse_when(text, today)
    if not parsed.title:
        raise ValueError("a task needs a title")
    proj = await _resolve_project(project, parsed.project_hint, parsed.title)
    read = {"date": parsed.due_date.isoformat() if parsed.due_date else None,
            "time": parsed.due_time.isoformat(timespec="minutes") if parsed.due_time else None,
            "spans": parsed.spans, "priority": parsed.priority,
            "rule": recurring.describe(parsed.rule) if parsed.rule else None}
    if parsed.rule:
        # A rule, not a task: the next occurrences are written as tasks now and
        # the daily job keeps the horizon filled from here on.
        rule = await recurring.add_rule(parsed.title, parsed.rule, project=proj, source=source, at=parsed.due_time)
        written = await recurring.materialize(today=today, rule_id=rule["id"])
        first = written[0] if written else None
        if first is None:
            # Nothing inside the horizon yet (an annual date months away): the rule still stands.
            return {"kind": "rule", "id": rule["id"], "title": rule["title"], "when": rule["next"], "at": None,
                    "priority": "normal", "status": "open", "done": False, "source": source, "project": proj,
                    "created": True, "rule": rule, "read": read}
        return {**first, "created": True, "rule": rule, "read": read}
    if source_ref:
        existing = await db.fetchrow(
            f"select {_TASK_COLS} {_TASK_FROM} where d.source = $1 and d.source_ref = $2 and lower(d.title) = lower($3)",
            source, source_ref, parsed.title,
        )
        if existing is not None:
            return {**_task(existing), "created": False}
    if source != "owner":
        # An agent that says the same thing twice gets the open task back, not a copy.
        existing = await db.fetchrow(
            f"select {_TASK_COLS} {_TASK_FROM} where d.status = 'open' and lower(d.title) = lower($1) limit 1",
            parsed.title,
        )
        if existing is not None:
            return {**_task(existing), "created": False, "read": read}
    due_at = None
    if parsed.due_date and parsed.due_time:
        due_at = datetime.combine(parsed.due_date, parsed.due_time, tzinfo=ZoneInfo(settings.timezone))
    row = await db.fetchrow(
        """
        insert into deadlines (title, due_date, due_at, priority, source, source_ref, project_id, definition_of_done, domain)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        returning id
        """,
        parsed.title, parsed.due_date, due_at, parsed.priority, source, source_ref,
        proj["id"] if proj else None, definition_of_done, proj["kind"] if proj else None,
    )
    task_id = row["id"]
    if parsed.due_date and parsed.due_date >= today:
        await _remind(task_id, parsed.title, parsed.due_date)
    task = await get_task(task_id)
    assert task is not None
    return {**task, "created": True, "read": read}


async def _remind(task_id: UUID, title: str, due: date) -> None:
    """The executive loop's ordinary reminder, 9 a.m. on the day."""
    try:
        trigger_id = await create_trigger(
            trigger_type="time",
            spec={"kind": "deadline_reminder", "deadline_id": str(task_id)},
            payload={"message": f"{title} is due today ({due.isoformat()})."},
            description=f"Deadline reminder: {title}",
            fires_at=_reminder_fires_at(due, 0),
        )
        await db.execute("update deadlines set trigger_id = $2 where id = $1", task_id, trigger_id)
    except Exception:
        log.exception("planner: could not arm the reminder for %s", task_id)


async def _resolve_project(explicit: str | UUID | None, hint: str | None, title: str) -> dict[str, Any] | None:
    if isinstance(explicit, UUID):
        row = await db.fetchrow("select id, slug, name, kind from projects where id = $1", explicit)
        return dict(row) if row else None
    if explicit:
        row = await db.fetchrow(
            "select id, slug, name, kind from projects where slug = $1 or lower(name) = lower($1) limit 1", str(explicit))
        if row:
            return dict(row)
    if hint:
        row = await db.fetchrow(
            "select id, slug, name, kind from projects where slug = $1 or slug like $1 || '%' order by length(slug) limit 1",
            hint)
        if row:
            return dict(row)
    low = f" {title.lower()} "
    rows = await db.fetch("select id, slug, name, kind from projects where status = 'active' order by priority, slug")
    for r in rows:
        if f" {r['slug'].lower()} " in low or (len(r["name"]) >= 4 and r["name"].lower() in low):
            return dict(r)
    return None


def _task(row: Any) -> dict[str, Any]:
    r = dict(row)
    project = ({"id": r["project_id"], "slug": r["project_slug"], "name": r["project_name"], "kind": r["project_kind"]}
               if r.get("project_id") else None)
    return {
        "kind": "task", "id": r["id"], "title": r["title"], "when": r["due_date"], "at": r["due_at"],
        "priority": r["priority"], "status": r["status"], "done": r["status"] == "done",
        "source": r["source"], "source_ref": r["source_ref"], "repeats": r["source"] == "recurring",
        "definition_of_done": r["definition_of_done"],
        "notes": r["notes"], "project": project, "created_at": r["created_at"], "completed_at": r["completed_at"],
    }


async def get_task(task_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(f"select {_TASK_COLS} {_TASK_FROM} where d.id = $1", task_id)
    return _task(row) if row else None


async def find_task(query: str) -> dict[str, Any] | None:
    """A task by id, id prefix, or the closest open title."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        return await get_task(UUID(q))
    except ValueError:
        pass
    if len(q) >= 8 and re.fullmatch(r"[0-9a-f-]+", q):
        rows = await db.fetch(f"select {_TASK_COLS} {_TASK_FROM} where d.id::text like $1 limit 2", f"{q}%")
        if len(rows) == 1:
            return _task(rows[0])
    rows = await db.fetch(
        f"select {_TASK_COLS} {_TASK_FROM} where d.status = 'open' and d.title ilike $1 "
        "order by (lower(d.title) = lower($2)) desc, d.due_date nulls last limit 2",
        f"%{q}%", q,
    )
    if len(rows) == 1 or (rows and rows[0]["title"].lower() == q.lower()):
        return _task(rows[0])
    return None


async def complete(task_id: UUID) -> dict[str, Any] | None:
    if not await complete_deadline(task_id, "done"):
        return None
    return await get_task(task_id)


async def drop(task_id: UUID) -> dict[str, Any] | None:
    if not await complete_deadline(task_id, "cancelled"):
        return None
    return await get_task(task_id)


async def reopen(task_id: UUID) -> dict[str, Any] | None:
    await db.execute("update deadlines set status = 'open', completed_at = null where id = $1", task_id)
    return await get_task(task_id)


async def reschedule(task_id: UUID, due_date: date | None, due_time: time | None = None) -> dict[str, Any] | None:
    task = await get_task(task_id)
    if task is None:
        return None
    due_at = (datetime.combine(due_date, due_time, tzinfo=ZoneInfo(settings.timezone))
              if due_date and due_time else None)
    await db.execute("update deadlines set due_date = $2, due_at = $3 where id = $1", task_id, due_date, due_at)
    if due_date and due_date >= today_local():
        await _remind(task_id, task["title"], due_date)
    return await get_task(task_id)


# ------------------------------------------------------------------ the agenda


async def agenda(start: date | None = None, days: int = 7, *, today: date | None = None) -> dict[str, Any]:
    """The week as the screen shows it: a bucket per day, plus what is
    overdue and what has no date yet."""
    today = today or today_local()
    start = start or today
    days = max(1, min(int(days), 31))
    end = start + timedelta(days=days - 1)
    buckets: dict[date, list[dict[str, Any]]] = {start + timedelta(days=i): [] for i in range(days)}
    overdue: list[dict[str, Any]] = []
    someday: list[dict[str, Any]] = []

    rows = await db.fetch(
        f"""
        select {_TASK_COLS} {_TASK_FROM}
        where (d.status = 'open' and (d.due_date is null or d.due_date <= $2))
           or (d.status = 'done' and d.due_date between $1 and $2)
        order by d.due_date nulls last, d.due_at nulls last,
                 case d.priority when 'high' then 0 when 'normal' then 1 else 2 end, d.created_at
        """,
        start, end,
    )
    for r in rows:
        item = _task(r)
        when = item["when"]
        if when is None:
            someday.append(item)
        elif when < today and not item["done"]:
            overdue.append(item)
        elif when in buckets:
            buckets[when].append(item)

    for item in await _dated_signals(start, end):
        buckets[item["when"]].append(item)
    for item in await _night_tasks(start, end):
        buckets[item["when"]].append(item)
    for item in await _next_actions(start, end):
        buckets[item["when"]].append(item)

    order = {k: i for i, k in enumerate(KINDS)}
    for items in buckets.values():
        items.sort(key=lambda it: (it["at"] is None, it["at"] or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
                                   order.get(it["kind"], 9), it["title"].lower()))
    return {
        "today": today, "start": start, "end": end, "days": [
            {"date": d, "label": f"{d.strftime('%a')} {d.day}", "today": d == today, "items": items}
            for d, items in buckets.items()
        ],
        "overdue": overdue, "someday": someday, "rules": await recurring.list_rules(today),
        "counts": {"today": len(buckets.get(today, [])), "week": sum(len(v) for v in buckets.values()),
                   "overdue": len(overdue), "someday": len(someday)},
    }


_WHEN_SUFFIX = re.compile(r"\s+on\s+[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{2}(?:\s+(\d{2}):(\d{2}))?$")


async def _dated_signals(start: date, end: date) -> list[dict[str, Any]]:
    """Canvas due dates and calendar events the senses already filed."""
    rows = await db.fetch(
        """
        select s.id, s.source, s.subject, s.deadline, s.url, s.raw, s.triage_status, s.occurred_at,
               s.private, s.private_rule,
               p.id as project_id, p.slug as project_slug, p.name as project_name, p.kind as project_kind
        from signals s left join projects p on p.id = s.project_id
        where s.source in ('canvas', 'gcal') and s.deadline between $1 and $2
          and s.triage_status <> 'ignored' and s.subject not like 'Cancelled:%'
        order by s.occurred_at desc
        """,
        start, end,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        raw = r["raw"] if isinstance(r["raw"], dict) else {}
        # A calendar event updated twice is two signals; the newest wins.
        key = f"{r['source']}:{raw.get('event_id') or raw.get('assignment_id') or r['id']}"
        if key in seen:
            continue
        seen.add(key)
        subject = r["subject"] or ""
        at = None
        m = _WHEN_SUFFIX.search(subject)
        if m:
            if m.group(1):
                at = datetime.combine(r["deadline"], time(int(m.group(1)), int(m.group(2))),
                                      tzinfo=ZoneInfo(settings.timezone))
            subject = subject[: m.start()]
        title = re.sub(r"^(Calendar|Canvas|Due|Assignment):\s*", "", subject).strip() or "(untitled)"
        project = ({"id": r["project_id"], "slug": r["project_slug"], "name": r["project_name"], "kind": r["project_kind"]}
                   if r["project_id"] else None)
        out.append({"kind": "event" if r["source"] == "gcal" else "class", "id": r["id"], "title": title,
                    "when": r["deadline"], "at": at, "status": r["triage_status"], "done": False,
                    "source": r["source"], "url": r["url"], "project": project,
                    "private": r["private"], "private_rule": r["private_rule"]})
    return out


def for_model(plan: Any) -> Any:
    """The agenda as a model may see it (core/privacy.py): a calendar item the
    gate hides keeps its time and loses its title and link. The Plan page and
    the Bridge read the agenda directly and show everything to Trilok."""
    from core import privacy

    if isinstance(plan, list):
        return [for_model(x) for x in plan]
    if not isinstance(plan, dict):
        return plan
    out = {k: for_model(v) for k, v in plan.items()}
    if "title" in out and "source" in out and not privacy.model_visible(out):
        out["title"] = privacy.model_view(out)["subject"]
        out["url"] = None
    return out


async def _night_tasks(start: date, end: date) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select t.id, t.night, t.title, t.status, t.agent, t.definition_of_done,
               p.id as project_id, p.slug as project_slug, p.name as project_name, p.kind as project_kind
        from night_tasks t left join projects p on p.id = t.project_id
        where t.night between $1 and $2 and t.status <> 'skipped'
        order by t.night, t.created_at
        """,
        start, end,
    )
    return [{"kind": "night", "id": r["id"], "title": r["title"], "when": r["night"], "at": None,
             "status": r["status"], "done": r["status"] == "done", "source": "night", "agent": r["agent"],
             "definition_of_done": r["definition_of_done"],
             "project": ({"id": r["project_id"], "slug": r["project_slug"], "name": r["project_name"],
                          "kind": r["project_kind"]} if r["project_id"] else None)}
            for r in rows]


async def _next_actions(start: date, end: date) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select id, slug, name, kind, next_action, next_action_due from projects
        where status = 'active' and next_action is not null and next_action_due between $1 and $2
        order by next_action_due, priority
        """,
        start, end,
    )
    return [{"kind": "next", "id": r["id"], "title": r["next_action"], "when": r["next_action_due"], "at": None,
             "status": "open", "done": False, "source": "project",
             "project": {"id": r["id"], "slug": r["slug"], "name": r["name"], "kind": r["kind"]}}
            for r in rows]


def text_agenda(plan: dict[str, Any]) -> str:
    """The agenda as a few lines for the CEO (and a push)."""
    lines: list[str] = []
    if plan["overdue"]:
        lines.append("Overdue: " + "; ".join(f"{t['title']} ({t['when'].isoformat()})" for t in plan["overdue"][:8]))
    for day in plan["days"]:
        if not day["items"]:
            continue
        label = "Today" if day["today"] else day["date"].strftime("%a %b %d")
        parts = []
        for it in day["items"]:
            at = it["at"].astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M ") if it.get("at") else ""
            tag = {"event": "cal", "class": "class", "night": "night", "next": "next"}.get(it["kind"], "")
            proj = f" #{it['project']['slug']}" if it.get("project") else ""
            done = " (done)" if it.get("done") else ""
            parts.append(f"{at}{it['title']}{proj}{(' [' + tag + ']') if tag else ''}{done}")
        lines.append(f"{label}: " + "; ".join(parts))
    if plan["someday"]:
        lines.append("Someday: " + "; ".join(t["title"] for t in plan["someday"][:8]))
    return "\n".join(lines) if lines else "Nothing on the Planner for these days."


__all__ = ["Parsed", "parse_when", "quick_add", "get_task", "find_task", "complete", "drop", "reopen",
           "reschedule", "agenda", "text_agenda", "today_local", "KINDS", "PRIORITIES"]
