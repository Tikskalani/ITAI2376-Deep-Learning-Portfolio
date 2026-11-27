"""Atlas -- late approvals that finish the job.

A gated tool call waits ATLAS_APPROVAL_WAIT_MIN for its card to be decided
(ten minutes by default) and then gives up: the agent reports what it
prepared, the ledger holds the call as `pending`, and the card stays open
for a day. Until now a yes that arrived in hour two was recorded and
nothing happened: the email the agent wrote was never sent, and Trilok had
to ask for it all over again.

A late yes now finishes the job. decide_approval() hands an approved card
that nobody is waiting on to start(), which runs the exact tool call the
card described: the same agent's tool, the same input, under the same
ledger key, so it runs once however many processes see the decision. The
run is audited like any other tool call (approved_by = user), announced
on the event stream, and reported to him as a card of its own. A late no
settles the ledger so the call is not raised again inside its unit of
work; an expired card settles it as expired so a later run may ask again.

sweep() is the scheduler's safety net for anything a web process did not
get to (it was restarted mid-finish, or the decision came by some other
route). It is the same finish() behind the same ledger claim, so the two
paths cannot both act.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from core import db
from core.runtime import ledger, policy

log = logging.getLogger(__name__)

# Finishes running in this process, so a test (or a shutdown) can wait for them.
_tasks: set[asyncio.Task[Any]] = set()


def start(approval_id: UUID) -> asyncio.Task[Any]:
    """Finish an approved call in the background. Never raises."""
    task = asyncio.create_task(finish(approval_id), name=f"late-approval-{approval_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain() -> None:
    """Wait for every finish started in this process (tests, shutdown)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def finish(approval_id: UUID) -> dict[str, Any] | None:
    """Run the gated call behind an approved card, once.

    Returns {"tool", "agent", "ok", "result"|"error"} when this call did the
    work, None when there was nothing to do: the requester was still waiting
    and ran it itself, the card was not a gated tool call, or another
    process got there first.
    """
    try:
        row = await db.fetchrow(
            """
            update tool_ledger l set status = 'running', attempts = l.attempts + 1, updated_at = now()
            from approvals a
            where l.approval_id = $1 and l.status = 'pending' and a.id = l.approval_id and a.status = 'approved'
            returning l.key, l.tool, l.agent_name, l.input, l.episode_id, a.action_summary
            """,
            approval_id,
        )
    except Exception:
        log.exception("late approval %s: could not claim the ledger row", approval_id)
        return None
    if row is None:
        return None
    tool, agent_name, episode_id = row["tool"], row["agent_name"], row["episode_id"]
    tool_input = _loads(row["input"])
    spec = _tool_for(agent_name, tool)
    if spec is None:
        # Nothing ran, so the row goes back to a state a later run may claim.
        await ledger.settle(row["key"], "failed", result={"error": f"{agent_name} has no tool {tool}"})
        await _report(approval_id, agent_name, tool, episode_id, ok=False,
                      detail=f"{agent_name} no longer has a tool called {tool}, so nothing ran. Ask again in chat.")
        return {"tool": tool, "agent": agent_name, "ok": False, "error": "no such tool"}

    # The policy may have changed while the card waited: a tool now denied,
    # unclassified, or behind an unreadable table does not run on an old yes.
    gate, pattern = await policy.gate_for(tool)
    if gate in policy.REFUSING:
        await ledger.settle(row["key"], "failed", result={"error": f"policy {gate}"})
        await policy.announce_refusal(tool, gate, pattern, agent=agent_name, episode_id=episode_id)
        await _report(approval_id, agent_name, tool, episode_id, ok=False,
                      detail=f"{tool} did not run: {policy.refusal_message(tool, gate, pattern)}")
        return {"tool": tool, "agent": agent_name, "ok": False, "error": f"policy {gate}"}

    try:
        result = await spec.handler(tool_input)
    except Exception as exc:  # noqa: BLE001 - the outcome is recorded, not raised
        err = f"{type(exc).__name__}: {exc}"
        log.exception("late approval %s: %s.%s failed", approval_id, agent_name, tool)
        # Fail safe, as in the live loop: a crash may have acted unless the
        # tool says it never started, and only then may it be tried again.
        started = not isinstance(exc, ledger.NotStarted)
        await ledger.settle(row["key"], "running" if started else "failed", result={"error": err})
        await _audit(agent_name, tool, tool_input, {"error": err, "late": True}, episode_id)
        await _report(approval_id, agent_name, tool, episode_id, ok=False,
                      detail=(f"{tool} failed after your yes: {err}"
                              + ("" if started else " Nothing was sent; it can be tried again.")))
        return {"tool": tool, "agent": agent_name, "ok": False, "error": err}

    safe = _jsonsafe(result)
    await ledger.settle(row["key"], "done", result=safe)
    await _audit(agent_name, tool, tool_input, {"result": safe, "late": True}, episode_id)
    await _report(approval_id, agent_name, tool, episode_id, ok=True,
                  detail=_summary(row["action_summary"], tool, safe))
    return {"tool": tool, "agent": agent_name, "ok": True, "result": safe}


async def settle_rejected(approval_id: UUID) -> None:
    """A late no: the call is not raised again inside its unit of work."""
    try:
        await db.execute(
            "update tool_ledger set status = 'rejected', updated_at = now() "
            "where approval_id = $1 and status = 'pending'",
            approval_id,
        )
    except Exception:
        log.exception("late approval %s: could not settle the rejection", approval_id)


async def sweep(limit: int = 20) -> int:
    """Scheduler job: finish approved calls nobody ran, settle the rest.

    Returns how many calls this sweep finished. Never raises."""
    try:
        await db.execute(
            """
            update tool_ledger l set status = a.status, updated_at = now()
            from approvals a
            where l.approval_id = a.id and l.status = 'pending' and a.status in ('rejected', 'expired')
            """
        )
        rows = await db.fetch(
            """
            select a.id from approvals a join tool_ledger l on l.approval_id = a.id
            where a.status = 'approved' and l.status = 'pending'
            order by a.resolved_at limit $1
            """,
            limit,
        )
    except Exception:
        log.exception("late approvals: sweep could not read the ledger")
        return 0
    done = 0
    for r in rows:
        if await finish(r["id"]) is not None:
            done += 1
    if done:
        log.info("late approvals: finished %d call(s) on the sweep", done)
    return done


# ------------------------------------------------------------------ helpers


def _tool_for(agent_name: str, tool: str) -> Any:
    from core.runtime.dispatcher import get_registration

    reg = get_registration(agent_name)
    if reg is None or reg.tools_factory is None:
        return None
    try:
        tools = reg.tools_factory()
    except Exception:
        log.exception("late approval: %s's tools could not be built", agent_name)
        return None
    return next((t for t in tools if t.name == tool), None)


async def _audit(agent_name: str, tool: str, tool_input: Any, outputs: dict[str, Any], episode_id: UUID | None) -> None:
    try:
        await db.execute(
            """
            insert into actions (agent_name, action_type, inputs, outputs, approved_by, episode_id)
            values ($1, 'tool_call', $2, $3, 'user', $4)
            """,
            agent_name, {"tool": tool, "input": tool_input, "late": True}, outputs, episode_id,
        )
    except Exception:
        log.exception("late approval: could not audit %s.%s", agent_name, tool)


async def _report(approval_id: UUID, agent_name: str, tool: str, episode_id: UUID | None, *, ok: bool, detail: str) -> None:
    """Tell him, and tell the floor."""
    from core import events
    from core.surfaces.notify import notify

    title = f"Done on your yes: {tool}" if ok else f"Could not finish: {tool}"
    await events.emit("approval.finished", agent=agent_name, episode_id=episode_id, title=title,
                      detail=detail, data={"approval_id": str(approval_id), "tool": tool, "ok": ok})
    try:
        await notify("approval", title, detail, priority="high" if not ok else "normal",
                     data={"approval_id": str(approval_id), "agent": agent_name, "finished": ok})
    except Exception:
        log.exception("late approval %s: could not notify", approval_id)


def _summary(action_summary: str | None, tool: str, result: Any) -> str:
    shown = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    if len(shown) > 400:
        shown = shown[:400] + "…"
    head = (action_summary or f"run {tool}").strip()
    return f"{head}: ran after you approved it.\n\n{shown}" if shown and shown != "null" else f"{head}: ran after you approved it."


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value if value is not None else {}


def _jsonsafe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return str(value)


__all__ = ["start", "finish", "drain", "settle_rejected", "sweep"]
