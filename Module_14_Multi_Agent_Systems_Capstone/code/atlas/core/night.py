"""The night shift: finished work waiting every morning.

The vision (docs/atlas-vision-2026-10-10.md, section 4) in one loop:

  evening   propose()        Atlas lists tonight's tasks, one per project
                             with a next action, each with a definition of
                             done. Trilok can add his own. One push.
  he taps   decide()         approve or skip; nothing runs otherwise.
  late      run_night()      each approved task goes to its agent inside
                             the nightly cap. The agent must end with
                             DONE: (and where the result is) or NEEDS YOU:.
                             The cap, or the daily cap, holds the rest.
  morning   morning_report() "4 done, 1 needs you, $1.20", a trace link per
                             task. He taps Accept or Rework on each result,
                             and that verdict is the Overnight-yield measure.

This is the API engine only. The Claude engine (Claude Code opening pull
requests overnight) waits on his decisions about repos and plan terms.
The agents keep every rule they already have: they prepare, the approval
gates decide what leaves, the ledger stops a retry from acting twice.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db, events
from core.config import settings
from core.costs import CostCapExceeded, cost_of_usage, model_for_agent
from core.projects import list_projects

log = logging.getLogger(__name__)

STATUSES = ("proposed", "approved", "skipped", "running", "done", "needs_you", "failed", "held")
DECISIONS = ("approve", "skip")
REVIEWS = ("accepted", "rework")
# Which specialist works a project's next action when the project names no owner.
AGENT_FOR_KIND = {"client": "client_delivery", "own": "build", "hobby": "build", "personal": "personal_ops",
                  "college": "academic", "atlas": "cto"}
_DONE = re.compile(r"^\s*\**\s*DONE\b", re.I)
_NEEDS = re.compile(r"^\s*\**\s*(NEEDS YOU|BLOCKED)\b", re.I)
_COLS = ("id, night, project_id, title, definition_of_done, agent, engine, budget_usd, status, proposed_by, "
         "episode_id, result, error, cost_usd, review, review_note, reviewed_at, reported_at, created_at, "
         "started_at, finished_at")


def _local(now: datetime | None) -> datetime:
    tz = ZoneInfo(settings.timezone)
    return (now or datetime.now(UTC)).astimezone(tz)


def night_of(now: datetime | None = None) -> date:
    """The night a moment belongs to: the evening's date until noon the next day."""
    local = _local(now)
    return local.date() if local.hour >= 12 else local.date() - timedelta(days=1)


def phase(now: datetime | None = None) -> str:
    """'evening' (planning and running) or 'morning' (results and review)."""
    return "evening" if _local(now).hour >= 12 else "morning"


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["cost_usd"] = str(d.get("cost_usd") or "0")
    d["budget_usd"] = str(d["budget_usd"]) if d.get("budget_usd") is not None else None
    return d


def _agent_for(kind: str | None, owner_agent: str | None) -> str:
    if owner_agent and owner_agent != "ceo":
        return owner_agent
    return AGENT_FOR_KIND.get(kind or "", "ceo")


def _known_agents() -> list[str]:
    from core.runtime.dispatcher import registered_agents

    return registered_agents()


# ------------------------------------------------------------------ planning


async def propose(now: datetime | None = None, *, limit: int = 6, notify_when_done: bool = True) -> list[dict[str, Any]]:
    """Tonight's suggestions: one task per active project with a next action,
    best project first, skipping anything already on tonight's list. No
    model call; the proposals are the project list, phrased as work."""
    night = night_of(now)
    existing = {(r["project_id"], r["title"]) for r in await db.fetch(
        "select project_id, title from night_tasks where night = $1", night)}
    added: list[dict[str, Any]] = []
    for p in await list_projects("active"):
        if len(added) >= limit:
            break
        action = (p.next_action or "").strip()
        if not action or (p.id, action) in existing:
            continue
        due = f" It is due {p.next_action_due.isoformat()}." if p.next_action_due else ""
        dod = (f"{action} is finished to the point Trilok can act on it in the morning: the draft, research "
               f"or record is saved where he will find it, and the reply says DONE: and where it is.{due}")
        row = await db.fetchrow(
            f"""
            insert into night_tasks (night, project_id, title, definition_of_done, agent, proposed_by)
            values ($1, $2, $3, $4, $5, 'atlas') returning {_COLS}
            """,
            night, p.id, action[:300], dod, _agent_for(p.kind, p.owner_agent),
        )
        added.append(_row(row))
    if added and notify_when_done:
        from core.surfaces.notify import notify

        lines = [f"- {t['title']} ({t['agent']})" for t in added]
        await notify("night_proposed", f"Tonight: {len(added)} task{'s' if len(added) != 1 else ''} proposed",
                     "\n".join(lines) + "\n\nApprove or skip on Home. Nothing runs without your yes.",
                     data={"view": "home"})
    return added


async def add_task(title: str, definition_of_done: str, *, project: str | None = None, agent: str | None = None,
                   budget_usd: float | None = None, proposed_by: str = "owner",
                   now: datetime | None = None) -> dict[str, Any]:
    """A task Trilok (or a Claude session) adds himself. His own start approved."""
    from core.projects import get_project

    title = (title or "").strip()
    dod = (definition_of_done or "").strip()
    if not title:
        raise ValueError("a task needs a title")
    if not dod:
        raise ValueError("a task needs a definition of done")
    proj = await get_project(project) if project else None
    if project and proj is None:
        raise ValueError(f"no project with slug {project!r}")
    agent = (agent or "").strip().lower() or _agent_for(proj.kind if proj else None, proj.owner_agent if proj else None)
    if agent not in _known_agents():
        raise ValueError(f"no agent named {agent!r}")
    # His own start approved; so does the CEO's, which only queues what he asked for in chat.
    status = "approved" if proposed_by in ("owner", "ceo") else "proposed"
    row = await db.fetchrow(
        f"""
        insert into night_tasks (night, project_id, title, definition_of_done, agent, budget_usd, status, proposed_by)
        values ($1, $2, $3, $4, $5, $6, $7, $8) returning {_COLS}
        """,
        night_of(now), proj.id if proj else None, title[:300], dod[:2000], agent,
        Decimal(str(budget_usd)) if budget_usd is not None else None, status, proposed_by,
    )
    return _row(row)


async def decide(task_id: UUID | str, decision: str) -> dict[str, Any] | None:
    """Approve (from proposed) or skip (from proposed or approved). None if it was not open."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    allowed = ("proposed",) if decision == "approve" else ("proposed", "approved")
    row = await db.fetchrow(
        f"update night_tasks set status = $2, updated_at = now() where id = $1 and status = any($3::text[]) "
        f"returning {_COLS}",
        UUID(str(task_id)), "approved" if decision == "approve" else "skipped", list(allowed),
    )
    return _row(row) if row else None


async def approve_all(now: datetime | None = None) -> int:
    res = await db.execute(
        "update night_tasks set status = 'approved', updated_at = now() where night = $1 and status = 'proposed'",
        night_of(now),
    )
    return int(res.split()[-1]) if res else 0


# ------------------------------------------------------------------ the run


def task_prompt(t: dict[str, Any], project_name: str | None) -> str:
    where = f"the project {project_name}" if project_name else "Trilok's work"
    return (
        f"Night shift task for {where}.\n\n"
        f"Task: {t['title']}\n"
        f"Definition of done: {t['definition_of_done']}\n\n"
        "Work it through now with your tools. Trilok is asleep: do not ask him anything mid-task. "
        "When it is finished, reply with one line that starts with DONE: and says where the result is "
        "(what you saved, drafted or recorded), then two or three lines of detail. If you cannot finish it "
        "without him, reply with one line that starts with NEEDS YOU: and say exactly what you need. "
        "Never send, submit, post or pay: prepare it and stop. Do not invent facts."
    )


async def _spent(night: date) -> Decimal:
    return Decimal(str(await db.fetchval(
        "select coalesce(sum(cost_usd), 0) from night_tasks where night = $1", night) or 0))


async def run_night(*, dispatch_fn: Any = None, now: datetime | None = None,
                    cap_usd: float | None = None) -> dict[str, Any]:
    """Work every approved task for tonight, best project first, inside the
    nightly cap. Returns the counts. Never raises."""
    from core.projects import get_project_by_id
    from core.runtime import ledger

    if dispatch_fn is None:
        from core.runtime.dispatcher import dispatch as dispatch_fn
    night = night_of(now)
    cap = Decimal(str(cap_usd if cap_usd is not None else settings.night_usd_cap))
    out: dict[str, Any] = {"night": night.isoformat(), "done": 0, "needs_you": 0, "failed": 0, "held": 0, "spent": "0"}
    tasks = await db.fetch(
        f"""
        select {_COLS} from night_tasks t
         where night = $1 and status = 'approved'
         order by (select priority from projects p where p.id = t.project_id) nulls last, created_at
        """,
        night,
    )
    spent = await _spent(night)
    daily_cap_hit = False
    for t in tasks:
        if daily_cap_hit or spent >= cap:
            why = "the daily cap was reached" if daily_cap_hit else f"the nightly cap was reached (${spent:.2f} of ${cap:.2f})"
            await db.execute("update night_tasks set status = 'held', error = $2, updated_at = now() where id = $1",
                             t["id"], why)
            out["held"] += 1
            continue
        proj = await get_project_by_id(t["project_id"]) if t["project_id"] else None
        await db.execute("update night_tasks set status = 'running', started_at = now(), updated_at = now() where id = $1",
                         t["id"])
        await events.emit("night.task", agent=t["agent"], title=t["title"],
                          data={"task_id": str(t["id"]), "status": "running"})
        try:
            with ledger.ensure_scope(f"night:{t['id']}"):
                result = await dispatch_fn(t["agent"], task_prompt(dict(t), proj.name if proj else None),
                                           dispatched_by="night", participants=["owner", t["agent"]])
        except CostCapExceeded as exc:
            daily_cap_hit = True
            await db.execute("update night_tasks set status = 'held', error = $2, updated_at = now() where id = $1",
                             t["id"], f"the daily cap was reached: {exc}")
            out["held"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad task must not end the night
            log.exception("night shift: %s failed", t["title"])
            await db.execute(
                "update night_tasks set status = 'failed', error = $2, finished_at = now(), updated_at = now() where id = $1",
                t["id"], f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            out["failed"] += 1
            await events.emit("night.task", agent=t["agent"], title=t["title"],
                              detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                              data={"task_id": str(t["id"]), "status": "failed"})
            continue
        text = (getattr(result, "text", "") or "").strip()
        cost = cost_of_usage(model_for_agent(t["agent"]), getattr(result, "usage", None) or {})
        if _DONE.match(text):
            status = "done"
        elif _NEEDS.match(text):
            status = "needs_you"
        else:
            status = "needs_you"
            text = "NEEDS YOU: the agent did not report DONE. Its reply:\n" + text
        await db.execute(
            "update night_tasks set status = $2, result = $3, episode_id = $4, cost_usd = $5, finished_at = now(), "
            "updated_at = now() where id = $1",
            t["id"], status, text[:8000], getattr(result, "episode_id", None), cost,
        )
        spent += cost
        out[status] += 1
        await events.emit("night.task", agent=t["agent"], episode_id=getattr(result, "episode_id", None),
                          title=t["title"], detail=text.splitlines()[0] if text else "",
                          data={"task_id": str(t["id"]), "status": status, "cost": str(cost)})
    out["spent"] = str(spent.quantize(Decimal("0.01")))
    try:
        from core.heartbeats import set_heartbeat

        await set_heartbeat("night/run", {**out, "at": datetime.now(UTC).isoformat()})
    except Exception:  # noqa: BLE001
        log.exception("night shift: could not write the heartbeat")
    log.info("night shift %s: %s", night, out)
    return out


# ------------------------------------------------------------------ the morning


async def morning_report(now: datetime | None = None, *, send: bool = True) -> str | None:
    """One push: what got done, what needs him, what it cost. None when the
    night had no results, or they were already reported."""
    night = night_of(now)
    rows = [dict(r) for r in await db.fetch(
        f"select {_COLS} from night_tasks where night = $1 and status in ('done', 'needs_you', 'failed', 'held') "
        "and reported_at is null order by status, created_at",
        night,
    )]
    if not rows:
        return None
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("done", "needs_you", "failed", "held")}
    spent = sum((Decimal(str(r["cost_usd"] or 0)) for r in rows), Decimal("0"))
    parts = [f"{counts['done']} done"]
    if counts["needs_you"]:
        parts.append(f"{counts['needs_you']} need{'s' if counts['needs_you'] == 1 else ''} you")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if counts["held"]:
        parts.append(f"{counts['held']} held")
    title = f"Night shift: {', '.join(parts)}, ${spent.quantize(Decimal('0.01'))}"
    marks = {"done": "+", "needs_you": "!", "failed": "x", "held": "-"}
    lines = []
    for r in rows:
        first = (r.get("result") or r.get("error") or "").strip().splitlines()
        detail = first[0][:160] if first else ""
        link = f" #run/{str(r['episode_id'])[:8]}" if r.get("episode_id") else ""
        lines.append(f"{marks[r['status']]} {r['title']} ({r['agent']}): {detail}{link}")
    body = "\n".join(lines) + "\n\nAccept or send back each result on Home."
    if send:
        from core.surfaces.notify import notify

        await notify("night_report", title, body, priority="high" if counts["needs_you"] else "normal",
                     data={"view": "home"})
    await db.execute("update night_tasks set reported_at = now() where id = any($1::uuid[])", [r["id"] for r in rows])
    return f"{title}\n{body}"


async def review(task_id: UUID | str, verdict: str, note: str | None = None) -> dict[str, Any] | None:
    """Trilok's morning verdict on a result. None if the task has no result."""
    if verdict not in REVIEWS:
        raise ValueError(f"verdict must be one of {REVIEWS}")
    row = await db.fetchrow(
        f"update night_tasks set review = $2, review_note = $3, reviewed_at = now(), updated_at = now() "
        f"where id = $1 and status in ('done', 'needs_you') returning {_COLS}",
        UUID(str(task_id)), verdict, (note or "").strip()[:1000] or None,
    )
    if row is None:
        return None
    task = _row(row)
    # The verdict lands on the run that did the work (outcome learning).
    from core import outcomes

    await outcomes.from_night_review(task, verdict, note)
    return task


async def yield_so_far() -> dict[str, int]:
    """Results he has judged, and how many he accepted: the Overnight-yield measure."""
    r = await db.fetchrow(
        "select count(*) filter (where review is not null) as reviewed, "
        "count(*) filter (where review = 'accepted') as accepted from night_tasks where status in ('done', 'needs_you')"
    )
    return {"reviewed": int(r["reviewed"] or 0), "accepted": int(r["accepted"] or 0)}


# ------------------------------------------------------------------ read


async def listing(now: datetime | None = None) -> dict[str, Any]:
    """Tonight's list (or this morning's results) plus the night before, for the Home card."""
    night = night_of(now)
    rows = [_row(r) for r in await db.fetch(
        f"select {_COLS}, (select name from projects p where p.id = t.project_id) as project_name "
        "from night_tasks t where night in ($1, $2) order by night desc, created_at",
        night, night - timedelta(days=1),
    )]
    return {
        "phase": phase(now),
        "night": night.isoformat(),
        "tasks": [r for r in rows if r["night"] == night],
        "previous": [r for r in rows if r["night"] != night],
        "cap": settings.night_usd_cap,
        "spent": str((await _spent(night)).quantize(Decimal("0.01"))),
        "agents": _known_agents(),
        "projects": [{"slug": p.slug, "name": p.name} for p in await list_projects("active")],
    }


# ------------------------------------------------------------------ the night as a film


def window(night: date) -> tuple[datetime, datetime]:
    """The shift's hours, local: 22:00 on the night's date to 09:00 the next morning."""
    tz = ZoneInfo(settings.timezone)
    start = datetime(night.year, night.month, night.day, 22, 0, tzinfo=tz)
    return start, start + timedelta(hours=11)


async def replay(night: date | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything that happened on one night, in order, with the tasks it
    belonged to: the material the morning film is cut from. Every event in
    the window counts, not only night-shift tasks: a run the queue made at
    7 a.m., an approval he answered at midnight, a screenshot the submitter
    took. Totals come from the events themselves."""
    from decimal import Decimal as _D

    night = night or night_of(now)
    start, end = window(night)
    evs = await events.recent(limit=2000, start=start, end=end)
    tasks = [_row(r) for r in await db.fetch(
        f"select {_COLS}, (select name from projects p where p.id = t.project_id) as project_name "
        "from night_tasks t where night = $1 order by created_at",
        night,
    )]
    runs = {e["episode_id"] for e in evs if e["kind"] == "run.started" and e.get("episode_id")}
    cost = sum((_D(str(e["data"].get("cost") or 0)) for e in evs if e["kind"] == "run.finished"), _D("0"))
    agents = sorted({e["agent"] for e in evs if e.get("agent") and e["agent"] not in ("owner",)})
    return {
        "night": night.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "tasks": tasks,
        "timeline": evs,
        "totals": {
            "events": len(evs),
            "runs": len(runs),
            "tool_calls": sum(1 for e in evs if e["kind"] == "run.tool"),
            "approvals_asked": sum(1 for e in evs if e["kind"] == "approval.asked"),
            "screenshots": sum(1 for e in evs if e["kind"] == "submit.shot"),
            "pushes": sum(1 for e in evs if e["kind"] == "notified"),
            "cost": str(cost.quantize(_D("0.01"))),
            "agents": agents,
            "done": sum(1 for t in tasks if t["status"] == "done"),
            "needs_you": sum(1 for t in tasks if t["status"] == "needs_you"),
        },
    }


# ------------------------------------------------------------------ scheduler entries


async def scheduled_propose() -> None:
    try:
        await propose()
    except Exception:  # noqa: BLE001
        log.exception("night shift: proposing failed")


async def scheduled_run() -> None:
    try:
        await run_night()
    except Exception:  # noqa: BLE001
        log.exception("night shift: the run failed")


async def scheduled_report() -> None:
    try:
        await morning_report()
    except Exception:  # noqa: BLE001
        log.exception("night shift: the report failed")


__all__ = ["AGENT_FOR_KIND", "DECISIONS", "REVIEWS", "STATUSES", "add_task", "approve_all", "decide", "listing",
           "morning_report", "night_of", "phase", "propose", "replay", "review", "run_night", "scheduled_propose",
           "scheduled_report", "scheduled_run", "task_prompt", "window", "yield_so_far"]
