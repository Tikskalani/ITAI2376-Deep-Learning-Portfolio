"""Atlas -- the executive loop.

A scheduled cycle that runs every ATLAS_LOOP_INTERVAL_MIN minutes. Each
tick does three things:

  * fires any prospective triggers that have come due
  * runs the senses cycle: connectors sync into signals, Haiku triages
    them, and urgent ones are routed (interrupt within budget, or a
    specialist prepares something, or hold for the brief)
  * counts active goals (the Phase 0 stub; projects replace goals as the
    unit of organization in R3)

run_executive_cycle never raises. A failure in one tick is logged and the
loop keeps running, so a single bad tick cannot stop the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from core import liveness
from core.config import settings
from core.goals import list_goals
from core.heartbeats import set_heartbeat
from core.loop.senses import run_senses_cycle
from core.memory import get_due_triggers, mark_fired
from core.runtime.dispatcher import dispatch
from core.surfaces.notify import notify

log = logging.getLogger(__name__)


async def _deliver_to_owner(text: str, *, kind: str = "reminder") -> None:
    """Notify the owner. The first line becomes the title."""
    first = text.strip().splitlines()[0] if text.strip() else "Atlas"
    await notify(kind=kind, title=first[:120], body=text, priority="normal")


async def _deliver_interrupt(text: str) -> None:
    """Urgency-3 signals from the senses cycle: a high-priority push."""
    first = text.strip().splitlines()[0] if text.strip() else "Atlas"
    await notify(kind="interrupt", title=first[:120], body=text, priority="high")


def _overdue(trigger: dict[str, Any], now: datetime | None = None) -> str:
    """'' when the trigger fires on time, else a phrase like
    'was due Sat Aug 30 (39 days ago)'. A trigger counts as late when the
    loop missed it by more than two intervals (Atlas was down or stalled)."""
    fires_at = trigger.get("fires_at")
    if not isinstance(fires_at, datetime):
        return ""
    if fires_at.tzinfo is None:
        fires_at = fires_at.replace(tzinfo=UTC)
    late = (now or datetime.now(UTC)) - fires_at
    if late <= timedelta(minutes=settings.loop_interval_min * 2 + 5):
        return ""
    try:
        when = fires_at.astimezone(ZoneInfo(settings.timezone)).strftime("%a %b %d %H:%M")
    except Exception:  # noqa: BLE001
        when = fires_at.strftime("%a %b %d %H:%M UTC")
    days = late.days
    ago = f"{days} day{'s' if days != 1 else ''} ago" if days >= 1 else f"{int(late.total_seconds() // 3600)} hours ago"
    return f"was due {when} ({ago})"


async def _fire_trigger(trigger: dict[str, Any]) -> None:
    """Fire one due trigger by its payload, then mark it fired.

    Three payload shapes are understood:
      {"agent": ..., "task": ...}        -> dispatch that agent
      {"workflow": ..., "owner_input": ...} -> run that workflow
      {"message": ...}                   -> send the owner a reminder

    A trigger the loop missed (Atlas was down when it came due) still fires,
    labeled overdue with its original due time, so a late reminder never
    reads as an on-time one.

    The trigger is marked fired even if firing failed, so a broken
    trigger does not re-fire on every tick. Recurring triggers are a
    later concern; Phase 0 treats every trigger as one-shot.
    """
    tid = trigger["id"]
    short = str(tid)[:8]
    payload = trigger.get("payload") or {}
    desc = trigger.get("description") or "(no description)"
    overdue = _overdue(trigger)
    log.info("firing trigger %s%s: %s", short, f" ({overdue})" if overdue else "", desc)

    try:
        if isinstance(payload, dict) and payload.get("agent"):
            agent = payload["agent"]
            task = payload.get("task") or payload.get("instruction") or desc
            if overdue:
                task = (
                    f"[Scheduled task, running late: it {overdue} and Atlas was not running then. "
                    f"Say so plainly and check whether it still applies.]\n\n{task}"
                )
            await dispatch(
                agent,
                task,
                dispatched_by="loop/executive",
                participants=["owner", agent],
            )
        elif isinstance(payload, dict) and payload.get("workflow"):
            # A durable run: the engine notifies the owner when it finishes
            # (or stops at an approval card), so nothing is delivered here.
            from core.workflow_engine import run_to_completion

            run = await run_to_completion(
                payload["workflow"], payload.get("owner_input", ""), trigger="trigger"
            )
            log.info("workflow %s run %s is %s", payload["workflow"], str(run["id"])[:8], run["status"])
        elif isinstance(payload, dict) and payload.get("message"):
            message = str(payload["message"]).strip()
            if overdue:
                await notify(
                    kind="reminder",
                    title=f"Overdue: {message.splitlines()[0][:110]}",
                    body=f"{message}\n\nThis {overdue}. Atlas was not running then, so it is late.",
                    priority="high",
                    data={"overdue": True, "trigger_id": str(tid)},
                )
            else:
                first, _, rest = message.partition("\n")
                await notify(
                    kind="reminder",
                    title=f"Reminder: {first[:110]}",
                    body=rest.strip() or (desc if desc != message and desc != "(no description)" else ""),
                    priority="normal",
                    data={"trigger_id": str(tid)},
                )
        else:
            log.warning("trigger %s has no actionable payload", short)
            await _deliver_to_owner(
                f"A trigger came due but had nothing to act on: {desc}"
            )
    except Exception:
        log.exception("trigger %s failed while firing", short)
    finally:
        await mark_fired(tid)


async def _fire_due_triggers() -> int:
    """Fire every trigger that has come due. Returns how many fired."""
    due = await get_due_triggers()
    for trigger in due:
        await _fire_trigger(trigger)
    return len(due)


async def _scan_goals() -> int:
    """Phase 0 goal scan: count the active goals (a deliberate stub)."""
    goals = await list_goals(status="active")
    log.info("executive scan: %d active goal(s)", len(goals))
    return len(goals)


async def run_executive_cycle() -> dict[str, int]:
    """One executive-loop tick. Never raises.

    Returns a small summary, {triggers_fired, active_goals}, for logging
    and tests. Each half of the tick is guarded on its own so a failure
    in one does not skip the other.
    """
    log.info("executive cycle starting")
    fired = 0
    active_goals = 0
    senses: dict[str, Any] = {}

    limit_s = settings.loop_stage_timeout_min * 60
    try:
        fired = await asyncio.wait_for(_fire_due_triggers(), timeout=limit_s)
    except TimeoutError:
        log.error("executive cycle: firing triggers ran past %.0f min; cut off", limit_s / 60)
    except Exception:
        log.exception("executive cycle: firing triggers failed")

    try:
        senses = await asyncio.wait_for(run_senses_cycle(_deliver_interrupt), timeout=limit_s)
    except TimeoutError:
        log.error("executive cycle: senses ran past %.0f min; cut off", limit_s / 60)
    except Exception:
        log.exception("executive cycle: senses cycle failed")

    try:
        active_goals = await _scan_goals()
    except Exception:
        log.exception("executive cycle: goal scan failed")

    route = senses.get("route") or {}
    triage = senses.get("triage") or {}
    log.info(
        "executive cycle done: %d trigger(s) fired, %s signal(s) triaged, "
        "%s interrupt(s), %s prepared, %d active goal(s)",
        fired,
        triage.get("triaged", 0),
        route.get("interrupted", 0),
        route.get("prepared", 0),
        active_goals,
    )
    summary = {
        "triggers_fired": fired,
        "active_goals": active_goals,
        "signals_triaged": int(triage.get("triaged", 0) or 0),
        "interrupts": int(route.get("interrupted", 0) or 0),
        "prepared": int(route.get("prepared", 0) or 0),
    }
    try:
        from core.reliability import tick_gap

        await set_heartbeat("loop/executive", {**summary, "senses": senses, **await tick_gap()})
    except Exception:
        # A heartbeat write failure must not break the loop.
        log.exception("could not write executive heartbeat")

    liveness.cycle_done()
    await ping_external_heartbeat()
    return summary


# Timeout for the dead-man ping. It is a tiny GET to a monitoring service;
# if it takes longer than this the tick should not wait on it.
_HEARTBEAT_TIMEOUT = httpx.Timeout(5.0)


async def ping_external_heartbeat(url: str | None = None) -> bool:
    """GET the healthchecks.io-style ping URL so an outside service knows
    Atlas is alive. Returns True on a 2xx, False otherwise. Never raises.

    The point of this ping is that it leaves the machine: when the laptop
    sleeps or the worker dies, the monitoring service notices the silence
    and alerts the owner through a path that does not depend on Atlas,
    Telegram, or this process. A blank URL disables it.
    """
    target = (url if url is not None else settings.heartbeat_url).strip()
    if not target:
        return False
    try:
        async with httpx.AsyncClient(timeout=_HEARTBEAT_TIMEOUT) as client:
            resp = await client.get(target)
    except httpx.HTTPError as exc:
        log.warning("external heartbeat ping failed: %s", exc)
        return False
    if resp.is_success:
        return True
    log.warning("external heartbeat ping returned %s", resp.status_code)
    return False


__all__ = ["run_executive_cycle", "ping_external_heartbeat"]
