"""Atlas -- the durable workflow engine.

workflows/<name>.yml stays the definition (versioned in git); this module
runs it as a persisted, resumable, approval-aware run in Postgres
(migration 013). State is written after every step, so a crash, a
restart, or a 24-hour approval wait resumes from the last completed step
instead of starting over. core/workflows.py keeps loading and the
one-shot run_workflow for callers that want a linear recipe to finish in
one call; this is the path for anything that waits.

Step types (the `type` key; default agent):

  agent       agent: <name>, task: <template>       one dispatch; retried
              up to max_attempts on the next tick
  approval    summary: <template>, payload: <template or mapping>
              opens an approval card and waits; approved continues,
              rejected or expired ends the run
  wait_until  until: <template ISO datetime> or delay_minutes: N
  condition   source: <template>, contains: <text> (or equals), on_false:
              stop | skip_next   (no branching beyond that; logic
              belongs in Python, not YAML)
  notify      title: <template>, body: <template>, kind: <text>

Templates resolve {today}, {month}, {owner_input}, {step_N_output} and
any key of the run's input. Every run ends with one notification to the
owner (done or failed) unless the recipe ends with its own notify step.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from core import db
from core.config import settings
from core.runtime import ledger
from core.workflows import _month_label, _resolve, _today_iso

log = logging.getLogger(__name__)

STEP_TYPES = ("agent", "approval", "wait_until", "condition", "notify")
RUN_ACTIVE = ("pending", "running", "waiting")
_MAX_ATTEMPTS = 3
_MAX_STEPS_PER_ADVANCE = 20


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def load_definition(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """The YAML as a dict with every step normalised to carry a type."""
    import yaml

    from core.workflows import WORKFLOWS_DIR

    base = root or WORKFLOWS_DIR
    path = base / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(f"no workflow named {name!r} at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"workflow {name!r}: top level must be a mapping")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"workflow {name!r}: 'steps' must be a non-empty list")
    steps: list[dict[str, Any]] = []
    for i, s in enumerate(raw_steps, start=1):
        if not isinstance(s, dict):
            raise ValueError(f"workflow {name!r}: step {i} must be a mapping")
        step = dict(s)
        step_type = (step.get("type") or ("agent" if step.get("agent") else "")).strip()
        if step_type not in STEP_TYPES:
            raise ValueError(f"workflow {name!r}: step {i} has unknown type {step_type!r}")
        step["type"] = step_type
        if step_type == "agent" and not (step.get("agent") and step.get("task")):
            raise ValueError(f"workflow {name!r}: step {i} needs agent and task")
        if step_type == "approval" and not step.get("summary"):
            raise ValueError(f"workflow {name!r}: step {i} needs summary")
        if step_type == "wait_until" and not (step.get("until") or step.get("delay_minutes")):
            raise ValueError(f"workflow {name!r}: step {i} needs until or delay_minutes")
        if step_type == "condition" and not step.get("source"):
            raise ValueError(f"workflow {name!r}: step {i} needs source")
        if step_type == "notify" and not step.get("title"):
            raise ValueError(f"workflow {name!r}: step {i} needs title")
        steps.append(step)
    return {
        "name": (data.get("name") or name).strip(),
        "description": (data.get("description") or "").strip(),
        "version": int(data.get("version") or 1),
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def start_run(
    name: str,
    owner_input: str = "",
    *,
    trigger: str = "manual",
    input: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    root: Path | None = None,
) -> UUID:
    """Persist a new run and its steps. Does not execute; call advance_run."""
    definition = load_definition(name, root=root)
    run_input = {"owner_input": owner_input or "", **(input or {})}
    run_id: UUID = await db.fetchval(
        """
        insert into workflow_runs (name, version, status, trigger, input, project_id)
        values ($1, $2, 'pending', $3, $4, $5) returning id
        """,
        definition["name"],
        definition["version"],
        trigger,
        run_input,
        project_id,
    )
    for i, step in enumerate(definition["steps"], start=1):
        await db.execute(
            "insert into workflow_steps (run_id, index, type, input) values ($1, $2, $3, $4)",
            run_id,
            i,
            step["type"],
            step,
        )
    log.info("workflow run %s started: %s (%d steps)", str(run_id)[:8], name, len(definition["steps"]))
    return run_id


async def get_run(run_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow("select * from workflow_runs where id = $1", run_id)
    return dict(row) if row else None


async def get_steps(run_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select * from workflow_steps where run_id = $1 order by index", run_id
    )
    return [dict(r) for r in rows]


async def list_runs(*, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if status:
        rows = await db.fetch(
            "select * from workflow_runs where status = $1 order by created_at desc limit $2",
            status, limit,
        )
    else:
        rows = await db.fetch("select * from workflow_runs order by created_at desc limit $1", limit)
    return [dict(r) for r in rows]


async def cancel_run(run_id: UUID, reason: str = "cancelled by owner") -> bool:
    res = await db.execute(
        """
        update workflow_runs set status = 'cancelled', error = $2, finished_at = now(), updated_at = now()
        where id = $1 and status in ('pending', 'running', 'waiting')
        """,
        run_id,
        reason,
    )
    return res.endswith("1")


# Fixed update statements: each column is a pair of parameters, a flag saying
# whether to set it and the value. Nothing is spliced into the SQL, and a
# column can still be set to null on purpose (error=None clears an error).
_RUN_COLUMNS = ("status", "current_step", "started_at", "finished_at", "final_output", "error")
_RUN_UPDATE = """
    update workflow_runs set
      status       = case when $2  then $3::text        else status end,
      current_step = case when $4  then $5::integer     else current_step end,
      started_at   = case when $6  then $7::timestamptz else started_at end,
      finished_at  = case when $8  then $9::timestamptz else finished_at end,
      final_output = case when $10 then $11::text       else final_output end,
      error        = case when $12 then $13::text       else error end,
      updated_at   = now()
    where id = $1
"""
_STEP_COLUMNS = ("status", "attempts", "started_at", "finished_at", "output", "error", "waiting_on")
_STEP_UPDATE = """
    update workflow_steps set
      status      = case when $2  then $3::text        else status end,
      attempts    = case when $4  then $5::integer     else attempts end,
      started_at  = case when $6  then $7::timestamptz else started_at end,
      finished_at = case when $8  then $9::timestamptz else finished_at end,
      output      = case when $10 then $11::jsonb      else output end,
      error       = case when $12 then $13::text       else error end,
      waiting_on  = case when $14 then $15::jsonb      else waiting_on end
    where id = $1
"""


def _flagged(fields: dict[str, Any], columns: tuple[str, ...]) -> list[Any]:
    """[set?, value] for each column in statement order; unknown names refused."""
    unknown = set(fields) - set(columns)
    if unknown or not fields:
        raise ValueError(f"cannot update columns {sorted(unknown) or 'none'}")
    args: list[Any] = []
    for c in columns:
        args += [c in fields, fields.get(c)]
    return args


async def _set_run(run_id: UUID, **fields: Any) -> None:
    await db.execute(_RUN_UPDATE, run_id, *_flagged(fields, _RUN_COLUMNS))


async def _set_step(step_id: UUID, **fields: Any) -> None:
    await db.execute(_STEP_UPDATE, step_id, *_flagged(fields, _STEP_COLUMNS))


async def _finish_run(run_id: UUID, status: str, *, final_output: str = "", error: str | None = None) -> None:
    await _set_run(run_id, status=status, final_output=final_output, error=error, finished_at=datetime.now(UTC))


def _variables(run: dict[str, Any], steps: list[dict[str, Any]], now: datetime | None = None) -> dict[str, str]:
    variables: dict[str, str] = {"today": _today_iso(now), "month": _month_label(now)}
    for k, v in (run.get("input") or {}).items():
        variables[str(k)] = v if isinstance(v, str) else json.dumps(v, default=str)
    for s in steps:
        if s["status"] in ("done", "skipped"):
            out = s.get("output") or {}
            variables[f"step_{s['index']}_output"] = str(out.get("text", "") if isinstance(out, dict) else out)
    return variables


def _resolve_any(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _resolve(value, variables)
    if isinstance(value, dict):
        return {k: _resolve_any(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_any(v, variables) for v in value]
    return value


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def advance_run(
    run_id: UUID,
    *,
    dispatch_fn: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run steps until one waits, the run ends, or the per-call budget is
    spent. Returns the run row afterwards. Never raises for step failures;
    they are recorded on the step and the run."""
    run = await get_run(run_id)
    if run is None:
        raise ValueError(f"no workflow run {run_id}")
    if run["status"] not in RUN_ACTIVE:
        return run
    if dispatch_fn is None:
        from core.runtime.dispatcher import dispatch as _dispatch

        dispatch_fn = _dispatch

    if run["status"] == "pending":
        await _set_run(run_id, status="running", started_at=datetime.now(UTC))

    for _ in range(_MAX_STEPS_PER_ADVANCE):
        steps = await get_steps(run_id)
        pending = [s for s in steps if s["status"] in ("pending", "waiting", "running")]
        if not pending:
            final = ""
            for s in reversed(steps):
                if s["type"] == "agent" and s["status"] == "done":
                    final = str((s.get("output") or {}).get("text", ""))
                    break
            await _finish_run(run_id, "done", final_output=final)
            await _notify_outcome(run_id, "done", final)
            return await get_run(run_id) or run
        step = pending[0]
        variables = _variables(run, steps, now)
        outcome = await _execute_step(run, step, variables, dispatch_fn, now)
        if outcome == "wait":
            await _set_run(run_id, status="waiting", current_step=step["index"])
            return await get_run(run_id) or run
        if outcome == "stop":
            return await get_run(run_id) or run
        await _set_run(run_id, status="running", current_step=step["index"])
    return await get_run(run_id) or run


async def _execute_step(
    run: dict[str, Any], step: dict[str, Any], variables: dict[str, str], dispatch_fn: Any, now: datetime | None
) -> str:
    """Execute one step. Returns 'continue', 'wait' or 'stop'."""
    spec = _resolve_any(step.get("input") or {}, variables)
    stype = step["type"]
    run_id, step_id = run["id"], step["id"]

    if stype == "agent":
        if step["status"] == "waiting":
            await _set_step(step_id, status="pending")
        attempts = int(step.get("attempts") or 0) + 1
        await _set_step(step_id, status="running", attempts=attempts, started_at=datetime.now(UTC))
        from core import events

        await events.emit("workflow.step", agent=spec["agent"], title=f"{run['name']} step {step['index']}",
                          detail=spec["task"], data={"run_id": str(run_id), "attempt": attempts})
        try:
            # Every attempt of this step shares one ledger scope, so a gated
            # action the first attempt took is not taken again by a retry.
            with ledger.ensure_scope(f"workflow:{run_id}:step:{step_id}"):
                result = await dispatch_fn(
                    spec["agent"],
                    spec["task"],
                    dispatched_by=f"workflow/{run['name']}",
                    participants=["owner", spec["agent"]],
                )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.warning("workflow %s step %d failed (attempt %d): %s", run["name"], step["index"], attempts, err)
            if attempts >= _MAX_ATTEMPTS:
                await _set_step(step_id, status="failed", error=err, finished_at=datetime.now(UTC))
                await _finish_run(run_id, "failed", error=f"step {step['index']} ({spec['agent']}): {err}")
                await _notify_outcome(run_id, "failed", err)
                return "stop"
            # Leave it for the next tick with backoff recorded on the step.
            retry_after = (now or datetime.now(UTC)) + timedelta(minutes=5 * attempts)
            await _set_step(step_id, status="waiting", error=err,
                            waiting_on={"retry_after": retry_after.isoformat()})
            await _set_run(run_id, status="waiting", current_step=step["index"])
            return "stop"
        text = (getattr(result, "text", "") or "").strip()
        await _set_step(step_id, status="done", output={"text": text}, error=None,
                        waiting_on=None, finished_at=datetime.now(UTC))
        return "continue"

    if stype == "approval":
        if step["status"] == "waiting":
            approval_id = (step.get("waiting_on") or {}).get("approval_id")
            row = await db.fetchrow("select status, action_payload from approvals where id = $1", UUID(approval_id)) if approval_id else None
            status = row["status"] if row else "expired"
            if status == "pending":
                return "wait"
            if status == "approved":
                await _set_step(step_id, status="done", output={"text": "approved", "status": status},
                                finished_at=datetime.now(UTC))
                return "continue"
            await _set_step(step_id, status="failed", output={"status": status}, finished_at=datetime.now(UTC))
            await _finish_run(run_id, "cancelled" if status == "rejected" else "failed",
                              error=f"approval {status} at step {step['index']}")
            await _notify_outcome(run_id, "cancelled" if status == "rejected" else "failed", f"approval {status}")
            return "stop"
        from core.runtime.approval import open_approval

        payload = spec.get("payload")
        if not isinstance(payload, dict):
            payload = {"text": payload if payload is not None else ""}
        approval_id = await open_approval(
            summary=str(spec["summary"])[:500],
            payload={"workflow": run["name"], "run_id": str(run_id), "step": step["index"], **payload},
            agent_name=f"workflow/{run['name']}",
        )
        await _set_step(step_id, status="waiting", waiting_on={"approval_id": str(approval_id)},
                        started_at=datetime.now(UTC))
        return "wait"

    if stype == "wait_until":
        if step["status"] == "waiting":
            until = datetime.fromisoformat((step.get("waiting_on") or {})["until"])
            if (now or datetime.now(UTC)) < until:
                return "wait"
            await _set_step(step_id, status="done", output={"text": until.isoformat()}, finished_at=datetime.now(UTC))
            return "continue"
        if spec.get("until"):
            until = datetime.fromisoformat(str(spec["until"]).replace("Z", "+00:00"))
            if until.tzinfo is None:
                from zoneinfo import ZoneInfo

                until = until.replace(tzinfo=ZoneInfo(settings.timezone))
        else:
            until = (now or datetime.now(UTC)) + timedelta(minutes=int(spec.get("delay_minutes") or 0))
        await _set_step(step_id, status="waiting", waiting_on={"until": until.isoformat()}, started_at=datetime.now(UTC))
        return "wait"

    if stype == "condition":
        source = str(spec.get("source") or "")
        if "contains" in spec:
            ok = str(spec["contains"]).lower() in source.lower()
        elif "equals" in spec:
            ok = source.strip() == str(spec["equals"]).strip()
        else:
            ok = bool(source.strip())
        await _set_step(step_id, status="done", output={"text": "true" if ok else "false"}, finished_at=datetime.now(UTC))
        if ok:
            return "continue"
        on_false = (spec.get("on_false") or "stop").strip()
        if on_false == "skip_next":
            await db.execute(
                "update workflow_steps set status = 'skipped', output = '{\"text\": \"skipped\"}' "
                "where run_id = $1 and index = $2 and status = 'pending'",
                run_id, step["index"] + 1,
            )
            return "continue"
        await _finish_run(run_id, "done", final_output=variables.get(f"step_{step['index'] - 1}_output", ""))
        await _notify_outcome(run_id, "done", "stopped: condition not met")
        return "stop"

    if stype == "notify":
        from core.surfaces.notify import notify

        try:
            await notify(kind=str(spec.get("kind") or "workflow"), title=str(spec["title"])[:200],
                         body=str(spec.get("body") or ""), priority=str(spec.get("priority") or "normal"),
                         data={"workflow": run["name"], "run_id": str(run_id)})
        except Exception as exc:
            log.warning("workflow %s notify step failed: %s", run["name"], exc)
        await _set_step(step_id, status="done", output={"text": str(spec.get("body") or "")}, finished_at=datetime.now(UTC))
        return "continue"

    await _set_step(step_id, status="failed", error=f"unknown type {stype}")
    await _finish_run(run_id, "failed", error=f"unknown step type {stype}")
    return "stop"


async def _notify_outcome(run_id: UUID, status: str, detail: str) -> None:
    """One notification per run end, unless the recipe ended with notify."""
    steps = await get_steps(run_id)
    if steps and steps[-1]["type"] == "notify" and steps[-1]["status"] == "done" and status == "done":
        return
    run = await get_run(run_id)
    if run is None:
        return
    try:
        from core.surfaces.notify import notify

        title = f"Workflow {run['name']} {status}"
        body = (detail or "")[:3000] or title
        await notify(kind="workflow", title=title, body=body,
                     priority="high" if status == "failed" else "normal",
                     data={"workflow": run["name"], "run_id": str(run_id), "status": status})
    except Exception:
        log.exception("could not notify workflow outcome for %s", run_id)


async def tick(*, dispatch_fn: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Scheduler job: advance every run that can move. Never raises."""
    out = {"advanced": 0, "still_waiting": 0, "finished": 0}
    try:
        rows = await db.fetch(
            "select id, status from workflow_runs where status in ('pending', 'running', 'waiting') "
            "order by created_at limit 50"
        )
    except Exception:
        log.exception("workflow tick: could not list runs")
        return out
    for r in rows:
        try:
            if r["status"] == "waiting" and not await _ready(r["id"], now):
                out["still_waiting"] += 1
                continue
            run = await advance_run(r["id"], dispatch_fn=dispatch_fn, now=now)
            out["advanced"] += 1
            if run["status"] in ("done", "failed", "cancelled"):
                out["finished"] += 1
        except Exception:
            log.exception("workflow tick: run %s failed to advance", r["id"])
    return out


async def _ready(run_id: UUID, now: datetime | None) -> bool:
    """Whether a waiting run has anything to do right now (cheap checks)."""
    steps = await get_steps(run_id)
    waiting = [s for s in steps if s["status"] == "waiting"]
    if not waiting:
        return True
    step = waiting[0]
    wo = step.get("waiting_on") or {}
    moment = now or datetime.now(UTC)
    if step["type"] == "agent" and wo.get("retry_after"):
        return moment >= datetime.fromisoformat(wo["retry_after"])
    if step["type"] == "wait_until" and wo.get("until"):
        return moment >= datetime.fromisoformat(wo["until"])
    if step["type"] == "approval" and wo.get("approval_id"):
        status = await db.fetchval("select status from approvals where id = $1", UUID(wo["approval_id"]))
        return status != "pending"
    return True


async def run_to_completion(
    name: str,
    owner_input: str = "",
    *,
    trigger: str = "manual",
    dispatch_fn: Any = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Start a run and advance it once. Linear recipes finish here; a
    recipe that waits returns its run row in status 'waiting' and the
    scheduler tick carries it the rest of the way."""
    run_id = await start_run(name, owner_input, trigger=trigger, root=root)
    return await advance_run(run_id, dispatch_fn=dispatch_fn)


__all__ = [
    "STEP_TYPES",
    "load_definition",
    "start_run",
    "advance_run",
    "run_to_completion",
    "tick",
    "get_run",
    "get_steps",
    "list_runs",
    "cancel_run",
]
