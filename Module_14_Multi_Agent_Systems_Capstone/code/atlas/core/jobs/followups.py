"""The daily job-search pass and the morning-brief block.

  * an application with no word for FOLLOW_UP_DAYS gets a follow-up next
    action (once), collected into one notification
  * one silent for GHOST_DAYS is marked ghosted (a reply revives it)
  * an application that reached interview gets a prep pack from the career
    agent, once, stored on the application
  * the morning brief gets the funnel, what is due, and the best new roles
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core import db
from core.jobs import config as jobs_config
from core.jobs import store

log = logging.getLogger(__name__)

FOLLOW_UP_DAYS = 7
GHOST_DAYS = 30


def _last_activity(app: dict[str, Any]) -> datetime | None:
    """The last time anything happened: a reply, else the application date.
    created_at only when neither exists (a seeded row is not new activity)."""
    stamps = [s for s in (app.get("last_contact_at"), app.get("applied_at")) if s] or \
        [s for s in (app.get("created_at"),) if s]
    vals = [datetime.fromisoformat(s) for s in stamps]
    return max(vals) if vals else None


def plan(apps: list[dict[str, Any]], now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Pure: which applications need a follow-up, which went quiet for good."""
    out: dict[str, list[dict[str, Any]]] = {"follow_up": [], "ghost": [], "prep": []}
    for a in apps:
        last = _last_activity(a)
        quiet = (now - last).days if last else 0
        if a["status"] in ("applied", "screening") and quiet >= GHOST_DAYS:
            out["ghost"].append(a)
        elif a["status"] in ("applied", "screening") and quiet >= FOLLOW_UP_DAYS and not a.get("next_action"):
            out["follow_up"].append({**a, "quiet_days": quiet})
        if a["status"] == "interview" and not (a.get("materials") or {}).get("prep"):
            out["prep"].append(a)
    return out


async def run_daily(now: datetime | None = None, *, prep: bool = True) -> dict[str, int]:
    now = now or datetime.now(UTC)
    apps = await store.list_applications("open", limit=300)
    todo = plan(apps, now)
    for a in todo["ghost"]:
        await store.set_status(a["id"], "ghosted", f"No reply in {GHOST_DAYS} days.")
    for a in todo["follow_up"]:
        await store.set_next_action(
            a["id"], f"Follow up: {a['quiet_days']} days since you last heard from {a['company']}", now
        )
    if todo["follow_up"]:
        from core.surfaces.notify import notify

        lines = [f"{a['company']}, {a['role']} ({a['quiet_days']} days quiet)" for a in todo["follow_up"]]
        await notify(
            kind="job",
            title=f"{len(lines)} application{'s' if len(lines) != 1 else ''} to follow up",
            body="\n".join(lines) + "\n\nAsk Atlas to draft the follow-ups; nothing is sent without you.",
            data={"applications": [a["id"] for a in todo["follow_up"]]},
        )
    prepped = 0
    if prep:
        for a in todo["prep"][:3]:
            prepped += int(await prepare_interview(a))
    return {"follow_up": len(todo["follow_up"]), "ghosted": len(todo["ghost"]), "prepped": prepped}


async def prepare_interview(app: dict[str, Any]) -> bool:
    """Have the career agent write the prep pack once; store and announce it."""
    from core.runtime.dispatcher import dispatch
    from core.surfaces.notify import notify

    task = (
        f"Write an interview prep pack for Trilok's {app['role']} interview at {app['company']}. "
        "Use your tools: get_application for the history, read_resume for his background, and web search "
        "for the company. One page: what the company sells and to whom, what this role does day to day, "
        "the five questions he is most likely to get with a STAR answer for each drawn only from his real "
        "work, three sharp questions for him to ask, and the gaps to be ready for. Save it with "
        f"save_materials (kind 'prep') on application {app['id']}, then reply with a two-line summary."
    )
    try:
        result = await dispatch("career", task, dispatched_by="jobs/followups", participants=["owner", "career"])
    except Exception:  # noqa: BLE001 - one failed pack must not stop the pass
        log.exception("interview prep failed for %s", app["company"])
        return False
    await notify(
        kind="job",
        title=f"Interview prep ready: {app['company']}",
        body=(result.text or "").strip()[:1500],
        data={"application_id": app["id"]},
    )
    return True


async def scheduled_daily() -> None:
    """Daily job. Never raises."""
    try:
        seeded = await store.seed_from_config(jobs_config.load())
        res = await run_daily()
        log.info("jobs daily: seeded %d, %s", seeded, res)
    except Exception:  # noqa: BLE001
        log.exception("jobs daily pass failed")


async def brief_block(mark: bool = True) -> str:
    """The job-search lines for the morning brief, or '' when there is nothing."""
    prof = jobs_config.profile()
    counts = await store.funnel()
    open_n = sum(counts[s] for s in store.OPEN)
    due = await db.fetch(
        "select company, role, next_action from job_applications where status = any($1::text[]) "
        "and next_action is not null and next_action_at <= now() order by next_action_at limit 5",
        list(store.OPEN),
    )
    fresh = await store.top_postings(int(prof.get("brief_min_score") or 70), 5, ("new",))
    if not (open_n or fresh):
        return ""
    lines = [
        "Job search: "
        + ", ".join(f"{counts[s]} {s}" for s in ("applied", "screening", "interview", "offer") if counts[s])
        + (f" ({counts['rejected']} closed with a no)" if counts["rejected"] else "")
    ]
    for r in due:
        lines.append(f"- Due: {r['next_action']} ({r['role']})")
    if fresh:
        lines.append("New roles worth a look:")
        for p in fresh:
            pay = f", ${p['salary_min'] // 1000}k-${p['salary_max'] // 1000}k" if p.get("salary_min") and p.get("salary_max") else ""
            lines.append(f"- {p['title']} at {p['company']} ({p['score']}{pay}): {p.get('reason') or ''} {p.get('url') or ''}".rstrip())
        if mark:
            await store.mark_shown([p["id"] for p in fresh])
    return "\n".join(lines)


__all__ = ["FOLLOW_UP_DAYS", "GHOST_DAYS", "brief_block", "plan", "prepare_interview", "run_daily", "scheduled_daily"]
