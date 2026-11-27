"""Atlas -- the senses cycle: sync, triage, route.

Runs inside every executive tick. Three steps, each guarded on its own:

  1. sync    every configured connector pulls what happened since its
             cursor into `signals` (GitHub, Canvas; Gmail and Calendar
             follow in R2b)
  2. triage  one Haiku call per batch sets project, urgency, importance,
             deadline and gist (core/loop/triage.py)
  3. route   urgency 3 interrupts Trilok now, inside a daily budget;
             urgency 2 dispatches the project's owner agent to prepare
             something for the next brief; everything else holds for the
             brief, which reads signals_for_brief()

Silence is the default: a tick with nothing urgent sends nothing. The
interrupt budget (ATLAS_INTERRUPT_BUDGET, default 2 a day) is counted
from the actions audit table, so a restart cannot reset it. Delivery is
injected by the caller so this module never imports a surface; today
the executive loop passes its owner-delivery function, which Session A
replaces with notify().
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core import db, privacy
from core.config import settings
from core.connectors.canvas import CanvasConnector
from core.connectors.gcal import GoogleCalendarConnector
from core.connectors.github import GitHubConnector
from core.connectors.gmail import GmailConnector
from core.loop.triage import triage_new_signals
from core.projects import get_project_by_id
from core.runtime.dispatcher import dispatch
from core.signals import list_signals, set_status

log = logging.getLogger(__name__)

Deliver = Callable[[str], Awaitable[None]]

# How many urgency-2 signals one tick may hand to specialists. Each is a
# Sonnet run; the cap keeps a noisy morning from turning into a bill.
_PREPARE_PER_TICK = 3
_ACTOR = "loop/senses"


def connectors() -> list[Any]:
    """The connectors to sync, in order. Each exposes name and sync()."""
    return [GitHubConnector(), CanvasConnector(), GmailConnector(), GoogleCalendarConnector()]


async def sync_connectors() -> dict[str, Any]:
    """Run every connector's sync. A failing connector is logged, not raised."""
    out: dict[str, Any] = {}
    for c in connectors():
        try:
            out[c.name] = await c.sync()
        except Exception as exc:
            log.exception("senses: %s sync failed", c.name)
            out[c.name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def interrupts_today() -> int:
    """How many interrupts have gone out today (owner's timezone)."""
    n = await db.fetchval(
        """
        select count(*) from actions
        where agent_name = $1 and action_type = 'interrupt'
          and (created_at at time zone $2)::date = (now() at time zone $2)::date
        """,
        _ACTOR,
        settings.timezone,
    )
    return int(n or 0)


def format_interrupt(row: dict[str, Any], project_name: str | None) -> str:
    """The push text for an urgency-3 signal: short, specific, with a link."""
    head = f"[{project_name}] " if project_name else ""
    gist = row.get("triage_notes") or row["subject"]
    lines = [f"{head}{gist}"]
    if row.get("deadline"):
        lines.append(f"Due {row['deadline'].isoformat()}.")
    if row.get("url"):
        lines.append(row["url"])
    return "\n".join(lines)


def format_prepare_task(row: dict[str, Any], project_name: str | None) -> str:
    """The task handed to a specialist for an urgency-2 signal."""
    where = f" on project {project_name}" if project_name else ""
    body = (row.get("body") or "")[:1500]
    return (
        f"A signal arrived{where} that needs something prepared before Trilok's "
        f"next brief.\n\nWhat happened: {row.get('triage_notes') or row['subject']}\n"
        f"Source: {row['source']}; actor: {row.get('actor') or 'unknown'}; "
        f"when: {row['occurred_at'].isoformat(timespec='minutes')}\n"
        f"Link: {row.get('url') or 'none'}\n"
        f"Details:\n{body or '(none)'}\n\n"
        "Prepare the next step for him: a draft reply, a summary with the decision he "
        "has to make, or a short checklist. Store anything durable with your memory "
        "tools. Do not send, post, or submit anything; he acts on what you prepare."
    )


async def route_signals(deliver: Deliver) -> dict[str, Any]:
    """Interrupt, prepare, or hold each triaged signal. Never raises."""
    out: dict[str, Any] = {"interrupted": 0, "held_over_budget": 0, "prepared": 0}

    # Urgency 3: interrupt, within budget.
    try:
        urgent = await list_signals("triaged", min_urgency=3, limit=20)
        budget = settings.interrupt_budget - await interrupts_today()
        for row in urgent:
            if budget <= 0:
                out["held_over_budget"] += 1
                continue
            project = await get_project_by_id(row["project_id"]) if row.get("project_id") else None
            text = format_interrupt(row, project.name if project else None)
            try:
                await deliver(text)
            except Exception:
                log.exception("senses: interrupt delivery failed for %s", row["id"])
                continue
            await db.execute(
                """
                insert into actions (agent_name, action_type, inputs, outputs, approved_by)
                values ($1, 'interrupt', $2, $3, 'auto')
                """,
                _ACTOR,
                {"signal_id": str(row["id"]), "project": project.slug if project else None},
                {"text": text},
            )
            await set_status([row["id"]], "actioned")
            budget -= 1
            out["interrupted"] += 1
        if out["held_over_budget"]:
            log.info(
                "senses: %d urgent signal(s) held for the brief; interrupt budget spent",
                out["held_over_budget"],
            )
    except Exception:
        log.exception("senses: interrupt routing failed")

    # Urgency 2: have the owning agent prepare something.
    try:
        prepare = [
            r for r in await list_signals("triaged", min_urgency=2, limit=50)
            if r["urgency"] == 2 and privacy.model_visible(r)  # a hidden signal is never handed to an agent
        ][:_PREPARE_PER_TICK]
        for row in prepare:
            project = await get_project_by_id(row["project_id"]) if row.get("project_id") else None
            agent = project.owner_agent if project else "ceo"
            task = format_prepare_task(row, project.name if project else None)
            try:
                await dispatch(
                    agent,
                    task,
                    dispatched_by=_ACTOR,
                    participants=["owner", agent],
                )
            except Exception:
                log.exception("senses: prepare dispatch to %s failed for %s", agent, row["id"])
                continue
            await set_status([row["id"]], "actioned")
            out["prepared"] += 1
    except Exception:
        log.exception("senses: prepare routing failed")

    return out


async def run_senses_cycle(deliver: Deliver) -> dict[str, Any]:
    """Sync, triage, route. Never raises; returns a summary for the heartbeat."""
    summary: dict[str, Any] = {}
    try:
        summary["sync"] = await sync_connectors()
    except Exception as exc:  # sync_connectors guards itself; belt and braces
        log.exception("senses: sync step failed")
        summary["sync"] = {"error": str(exc)}
    try:
        summary["triage"] = await triage_new_signals()
    except Exception as exc:
        log.exception("senses: triage step failed")
        summary["triage"] = {"error": str(exc)}
    try:
        summary["route"] = await route_signals(deliver)
    except Exception as exc:
        log.exception("senses: route step failed")
        summary["route"] = {"error": str(exc)}
    return summary


__all__ = [
    "Deliver",
    "connectors",
    "sync_connectors",
    "interrupts_today",
    "format_interrupt",
    "format_prepare_task",
    "route_signals",
    "run_senses_cycle",
]
