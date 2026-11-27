"""Atlas -- CEO agent tools.

The CEO's toolset spans five areas:
  * semantic memory   -- remember_fact, recall_facts
  * goals             -- create_goal, list_goals, close_goal
  * the decision log  -- log_decision, resolve_decision
  * lessons           -- record_lesson, recall_lessons
  * dispatch          -- dispatch_agent

build_tools() returns the ToolSpec list the runtime hands to the model.
The dispatcher calls it through the CEO's registration (see agents/ceo
register_ceo).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from core import db
from core.decisions import find_decision, log_decision, resolve_decision
from core.goals import close_goal, create_goal, find_goal, list_goals
from core.lessons import record_lesson
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    remember_fact_tool,
)
from core.runtime.dispatcher import dispatch
from core.workflows import list_workflows


def _parse_iso_date(raw: Any) -> tuple[date | None, str | None]:
    """Parse an optional ISO date argument. Returns (date, error_message)."""
    if not raw:
        return None, None
    try:
        return date.fromisoformat(str(raw).strip()), None
    except ValueError:
        return None, f"date must be ISO format YYYY-MM-DD, got {raw!r}"


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


def _goal_view(goal: dict[str, Any]) -> dict[str, Any]:
    """Trim a goal row to the fields worth showing the model."""
    return {
        "id": str(goal["id"]),
        "description": goal["description"],
        "status": goal["status"],
        "owner": goal["owner"],
        "success_criteria": goal["success_criteria"],
        "target_date": (
            goal["target_date"].isoformat() if goal["target_date"] else None
        ),
    }


async def _create_goal(args: dict[str, Any]) -> dict[str, Any]:
    """Create an active goal."""
    description = (args.get("description") or "").strip()
    if not description:
        return {"error": "description is required and cannot be empty"}

    target_date, err = _parse_iso_date(args.get("target_date"))
    if err:
        return {"error": f"target_{err}"}

    success_criteria = (args.get("success_criteria") or "").strip() or None
    goal_id = await create_goal(
        description=description,
        owner="ceo",
        success_criteria=success_criteria,
        target_date=target_date,
    )
    return {"goal_id": str(goal_id), "created": description}


async def _list_goals(args: dict[str, Any]) -> dict[str, Any]:
    """List goals, by default the active ones."""
    status = (args.get("status") or "active").strip().lower()
    goals = await list_goals(status=None if status == "all" else status)
    return {
        "status_filter": status,
        "count": len(goals),
        "goals": [_goal_view(g) for g in goals],
    }


async def _close_goal(args: dict[str, Any]) -> dict[str, Any]:
    """Close, pause, or abandon a goal."""
    raw = (args.get("goal_id") or "").strip()
    if not raw:
        return {"error": "goal_id is required"}
    goal = await find_goal(raw)
    if goal is None:
        return {"error": f"no goal matches {raw!r}"}

    status = (args.get("status") or "closed").strip().lower()
    if status not in ("paused", "closed", "abandoned"):
        return {
            "error": f"status must be paused, closed, or abandoned, "
            f"got {status!r}"
        }
    changed = await close_goal(goal["id"], status=status)
    return {
        "goal_id": str(goal["id"]),
        "description": goal["description"],
        "status": status,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# Decision journal
# ---------------------------------------------------------------------------


async def _log_decision(args: dict[str, Any]) -> dict[str, Any]:
    """Record a decision with its rationale and expected outcome."""
    summary = (args.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required and cannot be empty"}

    review_at, err = _parse_iso_date(args.get("review_at"))
    if err:
        return {"error": f"review_{err}"}

    decision_id = await log_decision(
        summary=summary,
        rationale=(args.get("rationale") or "").strip() or None,
        expected_outcome=(args.get("expected_outcome") or "").strip() or None,
        made_by="owner",
        domain=(args.get("domain") or "").strip() or None,
        review_at=review_at,
    )
    return {"decision_id": str(decision_id), "logged": summary}


async def _resolve_decision(args: dict[str, Any]) -> dict[str, Any]:
    """Close a decision with what happened and how it turned out."""
    raw = (args.get("decision_id") or "").strip()
    if not raw:
        return {"error": "decision_id is required"}
    decision = await find_decision(raw)
    if decision is None:
        return {"error": f"no decision matches {raw!r}"}

    outcome = (args.get("outcome") or "").strip()
    if not outcome:
        return {"error": "outcome is required"}

    assessment = (args.get("assessment") or "").strip().lower()
    if assessment not in ("right", "wrong", "mixed", "unclear"):
        return {
            "error": "assessment must be right, wrong, mixed, or unclear, "
            f"got {assessment!r}"
        }
    changed = await resolve_decision(
        decision["id"], outcome=outcome, assessment=assessment
    )
    return {
        "decision_id": str(decision["id"]),
        "summary": decision["summary"],
        "assessment": assessment,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------


async def _record_lesson(args: dict[str, Any]) -> dict[str, Any]:
    """Store a distilled lesson in the lessons store."""
    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "content is required and cannot be empty"}
    lesson_id = await record_lesson(
        content=content,
        domain=(args.get("domain") or "").strip() or None,
    )
    return {"lesson_id": str(lesson_id), "recorded": content}


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


_CREATE_GOAL = ToolSpec(
    name="create_goal",
    description=(
        "Create a goal you will track for Trilok. Use it when a real "
        "objective emerges in conversation: something with a clear outcome, "
        "worth checking on over time. Keep the description concrete. Add "
        "success_criteria when you know what 'done' looks like, and a "
        "target_date when there is a real deadline."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What the goal is, in one concrete sentence.",
            },
            "success_criteria": {
                "type": "string",
                "description": "What 'done' looks like. Optional.",
            },
            "target_date": {
                "type": "string",
                "description": "Deadline as ISO date YYYY-MM-DD. Optional.",
            },
        },
        "required": ["description"],
    },
    handler=_create_goal,
)

_LIST_GOALS = ToolSpec(
    name="list_goals",
    description=(
        "List the goals you track. Defaults to active goals. Pass status "
        "to filter (active, paused, closed, abandoned) or 'all' for every "
        "goal. Check this before creating a goal so you do not duplicate one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "active, paused, closed, abandoned, or all. "
                    "Default active."
                ),
            }
        },
        "required": [],
    },
    handler=_list_goals,
)

_CLOSE_GOAL = ToolSpec(
    name="close_goal",
    description=(
        "Move a goal out of active. Use 'closed' when it is done, "
        "'abandoned' when it is dropped, 'paused' when it is on hold. The "
        "goal_id may be a full id or its first 8+ characters from list_goals."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal_id": {
                "type": "string",
                "description": "Goal id, or its first 8+ characters.",
            },
            "status": {
                "type": "string",
                "description": "closed, abandoned, or paused. Default closed.",
            },
        },
        "required": ["goal_id"],
    },
    handler=_close_goal,
)

_LOG_DECISION = ToolSpec(
    name="log_decision",
    description=(
        "Record a decision Trilok has made, in the decision journal. Use it "
        "whenever a real call gets made: a supplier picked, a price set, a "
        "gig taken. Capture the rationale and what outcome is expected, and "
        "set review_at to when the call should be revisited. This is how "
        "Atlas later tells him whether a decision worked."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "What was decided, in one line.",
            },
            "rationale": {
                "type": "string",
                "description": "Why it was decided that way. Optional.",
            },
            "expected_outcome": {
                "type": "string",
                "description": "What you expect to happen as a result. Optional.",
            },
            "domain": {
                "type": "string",
                "description": "Area it belongs to, e.g. a project slug like demo-dental. Optional.",
            },
            "review_at": {
                "type": "string",
                "description": "When to revisit, ISO date YYYY-MM-DD. Optional.",
            },
        },
        "required": ["summary"],
    },
    handler=_log_decision,
)

_RESOLVE_DECISION = ToolSpec(
    name="resolve_decision",
    description=(
        "Close out a decision in the journal once its outcome is known. "
        "Record what actually happened and assess it honestly: right, "
        "wrong, mixed, or unclear. The decision_id may be a full id or its "
        "first 8+ characters."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "decision_id": {
                "type": "string",
                "description": "Decision id, or its first 8+ characters.",
            },
            "outcome": {
                "type": "string",
                "description": "What actually happened.",
            },
            "assessment": {
                "type": "string",
                "description": "right, wrong, mixed, or unclear.",
            },
        },
        "required": ["decision_id", "outcome", "assessment"],
    },
    handler=_resolve_decision,
)

_RECORD_LESSON = ToolSpec(
    name="record_lesson",
    description=(
        "Store a distilled lesson, the durable takeaway from something that "
        "went well or badly. Write one clear statement that should shape how "
        "Atlas acts next time. Lessons are how the system gets wiser, not "
        "just larger."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The lesson, as one self-contained statement.",
            },
            "domain": {
                "type": "string",
                "description": "Area it belongs to, e.g. a project slug like job-search. Optional.",
            },
        },
        "required": ["content"],
    },
    handler=_record_lesson,
)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _dispatch_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a specialist agent to do a piece of work; return its report."""
    agent = (args.get("agent") or "").strip().lower()
    task = (args.get("task") or "").strip()
    if not agent:
        return {"error": "agent is required"}
    if not task:
        return {"error": "task is required"}
    try:
        result = await dispatch(agent, task, dispatched_by="ceo")
    except Exception as exc:
        return {
            "error": f"could not dispatch {agent!r}: "
            f"{type(exc).__name__}: {exc}"
        }
    return {
        "agent": agent,
        "turns": result.turns,
        "stop_reason": result.stop_reason,
        "report": result.text,
    }


_DISPATCH_AGENT = ToolSpec(
    name="dispatch_agent",
    description=(
        "Hand a piece of work to a specialist agent and get its report "
        "back. Roster: client_delivery (client projects), career (job search: "
        "applications, roles, resumes, interview prep), "
        "academic (HCC coursework), coo (deadlines and calendar), cfo (money), "
        "cto (Atlas code), build (specs), news_discovery (learning loop), "
        "opportunity_scout (income leads), personal_ops (household and the "
        "family projects), property_ops (Example Property Group, on request). Name the "
        "project in the task. Give a clear, self-contained task. Tell Trilok "
        "before you dispatch unless he has already asked for it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "The agent name: client_delivery, career, academic, coo, cfo, cto, build, news_discovery, opportunity_scout, personal_ops, property_ops.",
            },
            "task": {
                "type": "string",
                "description": "What you want the agent to do, self-contained.",
            },
        },
        "required": ["agent", "task"],
    },
    handler=_dispatch_agent,
)


async def _run_workflow_handler(args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required (try /workflow list)"}
    owner_input = (args.get("owner_input") or "").strip()
    try:
        from core.workflow_engine import get_steps, run_to_completion

        run = await run_to_completion(name, owner_input, trigger="ceo")
        steps = await get_steps(run["id"])
    except FileNotFoundError:
        return {
            "error": f"no workflow named {name!r}",
            "available": list_workflows(),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    done = sum(1 for s in steps if s["status"] in ("done", "skipped"))
    return {
        "ok": run["status"] == "done",
        "status": run["status"],
        "run_id": str(run["id"]),
        "workflow": run["name"],
        "completed": done,
        "total": len(steps),
        "final_output": run.get("final_output") or "",
        "note": (
            "The run is waiting on Trilok's approval card; it continues on its own "
            "after he decides." if run["status"] == "waiting" else None
        ),
        "steps": [
            {
                "index": s["index"],
                "type": s["type"],
                "agent": (s.get("input") or {}).get("agent"),
                "status": s["status"],
                "output": str((s.get("output") or {}).get("text", ""))[:1500],
            }
            for s in steps
        ],
    }


_RUN_WORKFLOW = ToolSpec(
    name="run_workflow",
    description=(
        "Execute a named workflow defined in workflows/<name>.yml. Use it "
        "when a recurring multi-agent recipe is already codified (monthly "
        "P&L, restock decision, etc) rather than dispatching specialists "
        "one-off. Pass optional owner_input to thread free-text context "
        "into the recipe."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Workflow name (file stem under workflows/).",
            },
            "owner_input": {
                "type": "string",
                "description": "Optional free-text context exposed as {owner_input}.",
            },
        },
        "required": ["name"],
    },
    handler=_run_workflow_handler,
)


# ---------------------------------------------------------------------------
# The Planner: tasks in and the week out, straight from chat
# ---------------------------------------------------------------------------


async def _add_task(args: dict[str, Any]) -> dict[str, Any]:
    from core import planner
    from core.setup_checklist import chore_for

    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required: the task the way Trilok said it")
    chore = chore_for(text)
    if chore is not None:
        # Setup is a state, not a dated task: it sat overdue on the Plan for weeks.
        return {"added": False, "setup_item": chore,
                "note": "Not added: this is on the setup checklist in Settings, Connections, which shows its live status. "
                        "Point Trilok there instead of making a task."}
    task = await planner.quick_add(
        text,
        project=(args.get("project") or "").strip() or None,
        definition_of_done=(args.get("definition_of_done") or "").strip() or None,
        source="ceo",
    )
    return {
        "task_id": str(task["id"]), "title": task["title"],
        "due_date": task["when"].isoformat() if task.get("when") else None,
        "time": task["at"].isoformat() if task.get("at") else None,
        "priority": task["priority"],
        "project": task["project"]["slug"] if task.get("project") else None,
        "read_as": task.get("read"),
    }


async def _plan_week(args: dict[str, Any]) -> dict[str, Any]:
    from core import planner

    days = int(args.get("days") or 7)
    start, _err = _parse_iso_date(args.get("start"))
    plan = planner.for_model(await planner.agenda(start, max(1, min(days, 31))))  # privacy gate
    return {"agenda": planner.text_agenda(plan), "counts": plan["counts"]}


async def _complete_task(args: dict[str, Any]) -> dict[str, Any]:
    from core import planner

    task = await planner.find_task(str(args.get("task") or ""))
    if task is None:
        return {"error": "no single open task matched; ask which one, or use plan_week to see them"}
    done = await planner.complete(task["id"])
    return {"task_id": str(task["id"]), "title": task["title"], "status": done["status"] if done else "open"}


async def _queue_for_tonight(args: dict[str, Any]) -> dict[str, Any]:
    from core import night

    task = await night.add_task(
        str(args.get("title") or ""), str(args.get("definition_of_done") or ""),
        project=(args.get("project") or "").strip() or None,
        agent=(args.get("agent") or "").strip() or None,
        proposed_by="ceo",
    )
    return {"task_id": str(task["id"]), "night": str(task["night"]), "title": task["title"],
            "agent": task["agent"], "status": task["status"],
            "note": "Queued for tonight's shift; the morning report carries the result."}


async def _explain_run(args: dict[str, Any]) -> dict[str, Any]:
    from core.replay import trace

    raw = str(args.get("run_id") or "").strip()
    if not raw:
        return {"error": "run_id is required: a full episode id or its first 8+ characters"}
    out = await trace(raw, detail_chars=500)
    return out or {"error": f"no run matches {raw!r}"}


_QUEUE_FOR_TONIGHT = ToolSpec(
    name="queue_for_tonight",
    description=(
        "Put a task on tonight's night shift: Atlas runs it while Trilok sleeps, "
        "under the nightly budget, and the morning report carries the result. "
        "Use it when he says tonight, overnight, or while I sleep, or when the "
        "work can wait for the night. The definition of done is what the result "
        "will be judged against, so make it concrete."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The task in one line."},
            "definition_of_done": {"type": "string", "description": "What finished looks like, concretely."},
            "project": {"type": "string", "description": "Project slug. Optional."},
            "agent": {"type": "string", "description": "The specialist to do it (career, academic, coo, cfo, cto, build, client_delivery, personal_ops, property_ops, opportunity_scout, news_discovery). Optional; the COO by default."},
        },
        "required": ["title", "definition_of_done"],
    },
    handler=_queue_for_tonight,
)

_EXPLAIN_RUN = ToolSpec(
    name="explain_run",
    description=(
        "Why an Atlas run did what it did: what triggered it, every tool call "
        "with its input and result, what the ledger or an approval held it back "
        "from, the playbook in force, what it produced and remembered, and what "
        "it cost. Use it when Trilok asks why Atlas did something or what a run "
        "cost. run_id is a full episode id or its first 8+ characters."
    ),
    input_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "Episode id or an 8+ character prefix."}},
        "required": ["run_id"],
    },
    handler=_explain_run,
)

async def _rate_run(args: dict[str, Any]) -> dict[str, Any]:
    from core import outcomes

    raw = str(args.get("run_id") or "").strip()
    verdict = str(args.get("verdict") or "").strip().lower()
    if not raw or verdict not in ("good", "bad"):
        return {"error": "run_id and a verdict of good or bad are required"}
    try:
        episode_id = UUID(raw)
    except ValueError:
        rows = await db.fetch("select id from episodes where id::text like $1 limit 2", f"{raw}%") if len(raw) >= 8 else []
        if len(rows) != 1:
            return {"error": f"no single run matches {raw!r}; use explain_run or recall_episodes to find the id"}
        episode_id = rows[0]["id"]
    try:
        out = await outcomes.record(episode_id, verdict, kind="correction",
                                    note=(args.get("note") or "").strip() or None, source="owner/chat")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"recorded": True, "verdict": verdict, "skills_affected": len(out["skills"]),
            "eval_case_proposed": bool(out["candidate_id"])}


_RATE_RUN = ToolSpec(
    name="rate_run",
    description=(
        "Record Trilok's verdict on a run, in his words, when he says a result "
        "was wrong or right ('that lender draft was wrong, Daniel is the contact', "
        "'the status update was perfect'). The verdict lands on every playbook "
        "line that was in force in the run, and a bad one proposes an eval case "
        "so the mistake is not made twice. Find the run id with explain_run or "
        "recall_episodes first; the first eight characters are enough."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Episode id or an 8+ character prefix."},
            "verdict": {"type": "string", "enum": ["good", "bad"]},
            "note": {"type": "string", "description": "What was wrong or right, in his words. Optional but valuable."},
        },
        "required": ["run_id", "verdict"],
    },
    handler=_rate_run,
)

_ADD_TASK = ToolSpec(
    name="add_task",
    description=(
        "Put a task on Trilok's Planner, written the way he said it. The date, "
        "time, project (#slug) and priority (!!) are read out of the text: "
        "'Email the lender by Friday #kirkdale', 'Ethics outline due 10/17 3pm', "
        "'Look into the Tesla API' (no date: it lands under Someday). A rule makes "
        "it recurring: 'Pay the mortgage every month on the 1st', 'Estimated taxes "
        "every quarter on the 15th', 'Team review every Friday 3pm'. Use it the "
        "moment he asks you to remind him, track something, or put something on "
        "his calendar or list; do not ask him to repeat it in another form."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The task in his words, dates and all."},
            "project": {"type": "string", "description": "Project slug or name, when the text does not name it. Optional."},
            "definition_of_done": {"type": "string", "description": "What finished looks like, if he said. Optional."},
        },
        "required": ["text"],
    },
    handler=_add_task,
)

_PLAN_WEEK = ToolSpec(
    name="plan_week",
    description=(
        "Read Trilok's Planner: tasks, Canvas due dates, calendar events, "
        "night-shift tasks and project next actions, day by day, with what is "
        "overdue and what has no date. Use it for 'what's on this week', "
        "'what's due', 'am I free Thursday', or before proposing when to do something."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many days from the start (default 7, at most 31)."},
            "start": {"type": "string", "description": "YYYY-MM-DD to start from. Default today."},
        },
    },
    handler=_plan_week,
)

_COMPLETE_TASK = ToolSpec(
    name="complete_task",
    description=(
        "Mark a Planner task done when Trilok says he did it. Pass the task id "
        "from plan_week or enough of the title to match exactly one open task."
    ),
    input_schema={
        "type": "object",
        "properties": {"task": {"type": "string", "description": "Task id, id prefix, or title."}},
        "required": ["task"],
    },
    handler=_complete_task,
)


def build_tools() -> list[ToolSpec]:
    """Return the CEO's current toolset. Called once per dispatch."""
    return [
        remember_fact_tool("ceo"),
        recall_facts_tool(),
        _CREATE_GOAL,
        _LIST_GOALS,
        _CLOSE_GOAL,
        _LOG_DECISION,
        _RESOLVE_DECISION,
        _RECORD_LESSON,
        recall_lessons_tool(),
        _ADD_TASK,
        _PLAN_WEEK,
        _COMPLETE_TASK,
        _QUEUE_FOR_TONIGHT,
        _EXPLAIN_RUN,
        _RATE_RUN,
        _DISPATCH_AGENT,
        _RUN_WORKFLOW,
    ]


__all__ = ["build_tools"]
