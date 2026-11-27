"""Atlas -- workflow primitive.

A workflow is a YAML file naming a recurring multi-agent recipe. Each
step dispatches one agent with a templated task; outputs from earlier
steps are available to later ones via {step_N_output} placeholders.

YAML shape:

  name: monthly_pnl
  description: Monthly P&L brief
  steps:
    - agent: cfo
      task: |
        Summarize {month} P&L. Income, expenses, net, top variances.
    - agent: coo
      task: |
        Looking at this CFO summary:
        ---
        {step_1_output}
        ---
        Flag anything that conflicts with calendar commitments next month.
    - agent: ceo
      task: |
        Compose the monthly P&L brief from these inputs:
        CFO: {step_1_output}
        COO: {step_2_output}

Variables resolved at run time:
  {today}         -- ISO date string (UTC)
  {month}         -- "Month YYYY", e.g. "May 2026"
  {owner_input}   -- the free-text the owner provided (or empty string)
  {step_N_output} -- the text result of step N (1-indexed)

Design constraints:
  * YAML is a markup, not a Turing-complete language. No branching, no
    loops, no conditionals. If a recipe needs logic, write it in Python
    instead of stretching the YAML.
  * Each step is one dispatch. If one fails, the workflow halts and
    the partial outputs are returned so the owner can see how far we
    got. The CEO still gets to decide what to do with the partial.
  * No side effects -- writing to memory, sending messages, calling
    APIs all happen inside the dispatched agents. The workflow just
    sequences them.

This module is pure logic. Telegram /workflow command wires it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from core.config import REPO_ROOT

log = logging.getLogger(__name__)

WORKFLOWS_DIR = REPO_ROOT / "workflows"

# Match {var} placeholders, supporting simple names and step_N_output.
_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


@dataclass
class WorkflowStep:
    """One agent dispatch in a workflow."""

    agent: str
    task: str  # may contain {placeholders}


@dataclass
class Workflow:
    """A loaded workflow definition."""

    name: str
    description: str
    steps: list[WorkflowStep]


@dataclass
class StepResult:
    """The outcome of one step's dispatch."""

    agent: str
    task: str
    output: str
    ok: bool
    error: str | None = None


@dataclass
class WorkflowRun:
    """The outcome of a full workflow execution."""

    workflow: str
    completed: int
    total: int
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    final_output: str = ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_workflow(name: str, data: Any) -> Workflow:
    """Validate the parsed YAML and return a Workflow."""
    if not isinstance(data, dict):
        raise ValueError(f"workflow {name!r}: top level must be a mapping")

    description = (data.get("description") or "").strip()
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(
            f"workflow {name!r}: 'steps' must be a non-empty list"
        )

    steps: list[WorkflowStep] = []
    for i, s in enumerate(raw_steps, start=1):
        if not isinstance(s, dict):
            raise ValueError(
                f"workflow {name!r}: step {i} must be a mapping"
            )
        agent = (s.get("agent") or "").strip()
        task = (s.get("task") or "").strip()
        if not agent:
            raise ValueError(
                f"workflow {name!r}: step {i} missing 'agent'"
            )
        if not task:
            raise ValueError(
                f"workflow {name!r}: step {i} missing 'task'"
            )
        steps.append(WorkflowStep(agent=agent, task=task))

    return Workflow(
        name=(data.get("name") or name).strip(),
        description=description,
        steps=steps,
    )


def load_workflow(name: str, *, root: Path | None = None) -> Workflow:
    """Load workflows/<name>.yml and return a parsed Workflow.

    Raises FileNotFoundError if the file is missing, ValueError on
    malformed YAML or missing required fields.
    """
    base = root or WORKFLOWS_DIR
    path = base / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(
            f"no workflow named {name!r} at {path}"
        )
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return _parse_workflow(name, data)


def list_workflows(*, root: Path | None = None) -> list[str]:
    """Return the names of all workflows in the workflows directory."""
    base = root or WORKFLOWS_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.yml"))


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------


def _today_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).date().isoformat()


def _month_label(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%B %Y")


def _resolve(template: str, variables: dict[str, str]) -> str:
    """Replace {var} placeholders. Unknown placeholders pass through."""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return variables[key]
        return m.group(0)

    return _PLACEHOLDER.sub(repl, template)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def run_workflow(
    name: str,
    owner_input: str = "",
    *,
    root: Path | None = None,
    dispatch_fn: Any = None,
    now: datetime | None = None,
) -> WorkflowRun:
    """Execute the named workflow.

    dispatch_fn is the function to call per step; defaults to
    core.runtime.dispatcher.dispatch. Tests pass a fake.
    """
    workflow = load_workflow(name, root=root)

    if dispatch_fn is None:
        from core.runtime.dispatcher import dispatch as _dispatch
        dispatch_fn = _dispatch

    variables: dict[str, str] = {
        "today": _today_iso(now),
        "month": _month_label(now),
        "owner_input": owner_input or "",
    }

    results: list[StepResult] = []
    final_output = ""

    for i, step in enumerate(workflow.steps, start=1):
        task = _resolve(step.task, variables)
        try:
            result = await dispatch_fn(
                step.agent,
                task,
                dispatched_by=f"workflow/{workflow.name}",
                participants=["owner", step.agent],
            )
        except Exception as exc:
            log.exception(
                "workflow %s step %d (%s) failed",
                workflow.name,
                i,
                step.agent,
            )
            results.append(
                StepResult(
                    agent=step.agent,
                    task=task,
                    output="",
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return WorkflowRun(
                workflow=workflow.name,
                completed=len(results) - 1,
                total=len(workflow.steps),
                ok=False,
                steps=results,
                final_output=final_output,
            )

        output = (getattr(result, "text", "") or "").strip()
        results.append(
            StepResult(
                agent=step.agent, task=task, output=output, ok=True
            )
        )
        variables[f"step_{i}_output"] = output
        final_output = output

    return WorkflowRun(
        workflow=workflow.name,
        completed=len(results),
        total=len(workflow.steps),
        ok=True,
        steps=results,
        final_output=final_output,
    )


async def schedule_workflow(
    name: str,
    fires_at: datetime,
    owner_input: str = "",
    description: str | None = None,
) -> UUID:
    """Create a prospective trigger that fires the named workflow.

    fires_at must be a tz-aware datetime. The executive loop will pick
    the trigger up on its next scan after fires_at.
    """
    from uuid import UUID  # noqa: F401  (typing only)

    from core.memory.prospective import create_trigger

    # Validate the workflow exists before scheduling. A typo at create
    # time is friendlier than a silent fire-and-fail later.
    load_workflow(name)

    return await create_trigger(
        trigger_type="time",
        spec={"fires_at": fires_at.isoformat()},
        payload={"workflow": name, "owner_input": owner_input},
        description=description or f"Run workflow {name}",
        fires_at=fires_at,
    )


__all__ = [
    "Workflow",
    "WorkflowStep",
    "WorkflowRun",
    "StepResult",
    "WORKFLOWS_DIR",
    "load_workflow",
    "list_workflows",
    "run_workflow",
    "schedule_workflow",
]
