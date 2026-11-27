"""Atlas -- episode replay.

Read-only reconstruction of what happened during a single agent run.
Useful for debugging "what did the CFO actually do" when a result
looks off, and for diffing behavior across prompt versions when the
same task is re-dispatched.

The data lives in three places: episodes (the run summary, tokens,
participants, what_changed), actions (every tool call with inputs and
outputs), and the agent's response text (currently stored only in the
episode's outcome field). The replay stitches those into one
chronological view.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core import db

_EPISODE_COLS = (
    "id, started_at, ended_at, agent_name, participants, summary, "
    "outcome, what_changed, transcript_path, tags, status, result, project_id"
)
_ACTION_COLS = (
    "id, agent_name, action_type, inputs, outputs, approved_by, "
    "episode_id, goal_id, created_at"
)


async def find_episode(raw: str) -> dict[str, Any] | None:
    """Resolve an episode by full UUID or an 8+ character id prefix."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None
    if uid is not None:
        row = await db.fetchrow(
            f"select {_EPISODE_COLS} from episodes where id = $1", uid
        )
        return dict(row) if row else None
    if len(raw) < 8:
        return None
    rows = await db.fetch(
        f"select {_EPISODE_COLS} from episodes "
        "where id::text like $1 limit 2",
        f"{raw}%",
    )
    return dict(rows[0]) if len(rows) == 1 else None


async def episode_actions(episode_id: UUID) -> list[dict[str, Any]]:
    """Every action recorded under an episode, oldest first."""
    rows = await db.fetch(
        f"select {_ACTION_COLS} from actions "
        "where episode_id = $1 order by created_at",
        episode_id,
    )
    return [dict(r) for r in rows]


async def replay_record(raw: str) -> dict[str, Any] | None:
    """Fetch episode + its actions in one structure. None if not found."""
    episode = await find_episode(raw)
    if episode is None:
        return None
    actions = await episode_actions(episode["id"])
    return {"episode": episode, "actions": actions}


def _short_id(value: Any) -> str:
    """First 8 chars of a UUID, or '?' if absent."""
    s = str(value or "")
    return s[:8] if s else "?"


def _truncate(value: Any, limit: int) -> str:
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def format_replay(record: dict[str, Any], detail_chars: int = 240) -> str:
    """Render an episode + its actions as a human-readable transcript.

    detail_chars caps the visible length of each action's inputs and
    outputs in the rendering; a downstream JSON dump is available via
    the script's --json mode for the full payloads.
    """
    e = record["episode"]
    actions = record["actions"]

    started = e["started_at"]
    ended = e["ended_at"]
    duration = None
    if started and ended:
        duration = (ended - started).total_seconds()

    what_changed = e.get("what_changed") or {}
    tokens = what_changed.get("tokens") or {}

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Episode {e['id']}")
    lines.append(f"Agent:        {e['agent_name'] or '?'}")
    lines.append(
        f"Started:      {started.isoformat() if started else '?'}"
    )
    lines.append(
        f"Ended:        {ended.isoformat() if ended else 'still open'}"
        + (f" ({duration:.1f}s)" if duration is not None else "")
    )
    if e["participants"]:
        lines.append(f"Participants: {', '.join(e['participants'])}")
    if tokens:
        parts = []
        for kind in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            v = tokens.get(kind)
            if v:
                parts.append(f"{kind.replace('_tokens','').replace('_input','/cache')}={v}")
        if parts:
            lines.append("Tokens:       " + ", ".join(parts))
    if e["summary"]:
        lines.append(f"Summary:      {e['summary']}")
    if e["outcome"]:
        lines.append(f"Outcome:      {e['outcome']}")
    if e["tags"]:
        lines.append(f"Tags:         {', '.join(e['tags'])}")
    lines.append("")
    lines.append(f"Actions ({len(actions)}):")
    lines.append("-" * 72)

    if not actions:
        lines.append("  (no tool calls recorded under this episode)")
    else:
        for idx, action in enumerate(actions, start=1):
            ts = action["created_at"].strftime("%H:%M:%S") if action["created_at"] else "?"
            approver = action.get("approved_by") or "auto"
            agent = action.get("agent_name") or "?"
            kind = action.get("action_type") or "?"
            lines.append(
                f"[{idx:>2}] {ts}  {agent}.{kind}  ({approver}-approved)"
            )
            inputs = action.get("inputs")
            outputs = action.get("outputs")
            if inputs:
                lines.append(f"     inputs:  {_truncate(inputs, detail_chars)}")
            if outputs:
                lines.append(f"     outputs: {_truncate(outputs, detail_chars)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The trace: why a run did what it did
# ---------------------------------------------------------------------------
#
# The replay above is a transcript. The trace is the accountable version:
# what triggered the run, every tool call with its input and result, what
# the ledger or an approval held it back from, the learned playbook that
# was in force, what it produced and remembered, what it cost, and the
# flags a reader should see first (refused, errored, waiting on Trilok).
# Everything comes from tables Atlas already writes; nothing is inferred
# from the model's text.


def to_json(value: Any) -> Any:
    """Datetimes, UUIDs and Decimals as JSON-safe values, recursively."""
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _cap(value: Any, limit: int | None) -> Any:
    """Keep a payload readable: strings over `limit` are cut with a marker,
    structures are serialized and cut the same way. None keeps everything."""
    if limit is None or value is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    text = json.dumps(to_json(value), ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[: limit - 1] + "…"


def _step(action: dict[str, Any], limit: int | None) -> dict[str, Any]:
    inputs = action.get("inputs") or {}
    outputs = action.get("outputs") or {}
    error = outputs.get("error") if isinstance(outputs, dict) else None
    if isinstance(outputs, dict) and "result" in outputs:
        result = outputs["result"]
    else:
        result = None if error else outputs
    approved_by = action.get("approved_by") or "auto"
    return {
        "id": action["id"],
        "at": action.get("created_at"),
        "tool": inputs.get("tool") or inputs.get("name") or "?",
        "input": _cap(inputs.get("input", inputs), limit),
        "result": _cap(result, limit),
        "error": error,
        "ok": error is None,
        "approved_by": approved_by,
        "gated": approved_by != "auto",
    }


def _flags(e: dict[str, Any], stop_reason: str | None, steps: list[dict[str, Any]],
           ledger: list[dict[str, Any]], approvals: list[dict[str, Any]]) -> list[str]:
    """What a reader should see first. Each flag is a plain sentence."""
    flags: list[str] = []
    if stop_reason == "refusal":
        flags.append("the model refused")
    if stop_reason == "max_turns":
        flags.append("hit the turn cap before finishing")
    if e.get("status") == "failed":
        flags.append("the run failed")
    elif e.get("status") == "incomplete":
        flags.append("the run did not finish")
    errors = sum(1 for s in steps if not s["ok"])
    if errors:
        flags.append(f"{errors} tool call{'s' if errors != 1 else ''} errored")
    if any(s["gated"] for s in steps):
        flags.append("an action went through an approval")
    for r in ledger:
        if r["status"] == "pending":
            flags.append("waiting for your approval")
        elif r["status"] == "rejected":
            flags.append("you said no to an action")
        elif r["status"] == "running":
            flags.append("an action's outcome is unknown")
    if any(a["status"] == "rejected" for a in approvals) and "you said no to an action" not in flags:
        flags.append("you said no to an action")
    if not e.get("ended_at"):
        flags.append("still running")
    return flags


async def trace(raw: str, *, detail_chars: int | None = 6000) -> dict[str, Any] | None:
    """Everything that explains one run, JSON-safe. None if no episode
    matches `raw` (a full id or an 8+ character prefix)."""
    from core.costs import cost_of_usage, model_for_agent

    e = await find_episode(raw)
    if e is None:
        return None
    eid: UUID = e["id"]
    what = e.get("what_changed") or {}
    tokens = what.get("tokens") or {}
    model = model_for_agent(e.get("agent_name"))

    project = None
    if e.get("project_id"):
        project = await db.fetchrow("select id, slug, name from projects where id = $1", e["project_id"])

    dispatch = await db.fetchrow(
        "select agent_name, inputs, outputs, created_at from actions "
        "where episode_id = $1 and action_type = 'dispatch' order by created_at limit 1",
        eid,
    )
    steps = [_step(a, detail_chars) for a in await episode_actions(eid) if a.get("action_type") == "tool_call"]

    ledger = [dict(r) for r in await db.fetch(
        "select key, tool, status, attempts, input, result, approval_id, created_at, updated_at "
        "from tool_ledger where episode_id = $1 order by created_at",
        eid,
    )]
    approval_ids = [r["approval_id"] for r in ledger if r.get("approval_id")]
    approvals = [dict(r) for r in await db.fetch(
        "select id, action_summary, status, decided_by, created_at, resolved_at, expires_at "
        "from approvals where id = any($1::uuid[]) order by created_at",
        approval_ids,
    )] if approval_ids else []

    skills = [dict(r) for r in await db.fetch(
        "select s.id, s.title, s.slug, s.status from skill_uses u join skills s on s.id = u.skill_id "
        "where u.episode_id = $1 order by u.created_at",
        eid,
    )]
    feedback = [dict(r) for r in await db.fetch(
        "select verdict, note, source, created_at from episode_feedback where episode_id = $1 order by created_at",
        eid,
    )]
    lessons = [dict(r) for r in await db.fetch(
        "select id, content, domain from lessons where source_episode = $1 order by created_at", eid,
    )]
    facts = [dict(r) for r in await db.fetch(
        "select id, content from facts where episode_id = $1 order by created_at", eid,
    )]
    decisions = [dict(r) for r in await db.fetch(
        "select id, summary from decisions where episode_id = $1 order by decided_at", eid,
    )]
    # Runs this one dispatched: the dispatch action is written under the
    # child's episode, by this agent's name, while this run was open.
    children = [dict(r) for r in await db.fetch(
        """
        select c.id, c.agent_name, c.summary, c.started_at from actions a
          join episodes c on c.id = a.episode_id
         where a.action_type = 'dispatch' and a.agent_name = $1 and a.episode_id <> $2
           and a.created_at >= $3 and a.created_at <= coalesce($4, now())
         order by a.created_at
        """,
        e.get("agent_name") or "", eid, e["started_at"], e.get("ended_at"),
    )] if e.get("agent_name") else []

    # Older runs kept turns and stop reason only on their dispatch record.
    stop_reason = what.get("stop_reason")
    turns = what.get("turns")
    if dispatch and isinstance(dispatch.get("outputs"), dict):
        stop_reason = stop_reason or dispatch["outputs"].get("stop_reason")
        turns = turns if turns is not None else dispatch["outputs"].get("turns")

    started, ended = e.get("started_at"), e.get("ended_at")
    return to_json({
        "episode": {
            "id": eid, "agent": e.get("agent_name"), "started_at": started, "ended_at": ended,
            "duration_s": (ended - started).total_seconds() if started and ended else None,
            "summary": e.get("summary"), "outcome": e.get("outcome"), "status": e.get("status"),
            "result": _cap(e.get("result"), detail_chars), "participants": list(e.get("participants") or []),
            "tags": list(e.get("tags") or []), "turns": turns, "stop_reason": stop_reason,
            "tools_used": list(what.get("tools_used") or []),
        },
        "project": dict(project) if project else None,
        "trigger": {
            "by": dispatch["agent_name"], "at": dispatch["created_at"],
            "task": _cap((dispatch.get("inputs") or {}).get("task"), detail_chars),
        } if dispatch else None,
        "steps": steps,
        "ledger": [{**r, "input": _cap(r.get("input"), detail_chars), "result": _cap(r.get("result"), detail_chars)}
                   for r in ledger],
        "approvals": approvals,
        "skills": skills,
        "feedback": feedback,
        "produced": {"lessons": lessons, "facts": facts, "decisions": decisions, "children": children},
        "cost": {"model": model, "tokens": tokens, "usd": cost_of_usage(model, tokens)},
        "flags": _flags(e, stop_reason, steps, ledger, approvals),
    })


__all__ = [
    "find_episode",
    "episode_actions",
    "replay_record",
    "format_replay",
    "to_json",
    "trace",
]
