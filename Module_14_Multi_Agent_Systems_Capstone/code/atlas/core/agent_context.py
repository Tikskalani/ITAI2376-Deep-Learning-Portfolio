"""Atlas -- per-agent working context.

Specialists in Atlas have historically been stateless: each dispatch
started blank. That meant a CFO asked twice about the same statement
would not know it had been asked before. This module gives every agent
a thin "what I last did" memory.

Two functions:
  * record_context(agent, summary, episode_id=None) writes a one-line
    summary at episode end.
  * recent_context(agent, limit=3) returns the most recent summaries
    for that agent, newest first, joined as a single string for direct
    injection into the next dispatch's memory_context.

Lightweight on purpose. If summaries get too dense, the hygiene job
can prune them; for now we trust the 200-char cap to keep things tidy.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db
from core.config import settings

log = logging.getLogger(__name__)

_SUMMARY_MAX = 240


async def record_context(
    agent: str,
    summary: str,
    *,
    episode_id: UUID | None = None,
) -> UUID | None:
    """Write one working-context line for an agent. Never raises."""
    summary = (summary or "").strip()
    if not summary or not agent:
        return None
    if len(summary) > _SUMMARY_MAX:
        summary = summary[: _SUMMARY_MAX - 3] + "..."
    try:
        row = await db.fetchrow(
            """
            insert into agent_context (agent_name, episode_id, summary)
            values ($1, $2, $3)
            returning id
            """,
            agent,
            episode_id,
            summary,
        )
        return row["id"]
    except Exception:
        log.exception("agent_context: insert failed for %r", agent)
        return None


async def recent_context(agent: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return the most recent N context rows for an agent. Never raises."""
    try:
        rows = await db.fetch(
            """
            select id, summary, created_at, episode_id
            from agent_context
            where agent_name = $1
            order by created_at desc
            limit $2
            """,
            agent,
            max(1, min(int(limit or 3), 10)),
        )
        return [dict(r) for r in rows]
    except Exception:
        log.exception("agent_context: fetch failed for %r", agent)
        return []


async def recent_context_block(agent: str, limit: int = 3) -> str:
    """Format recent context as a single text block for memory_context.

    Returns the empty string when nothing is recorded, so callers can
    safely concatenate without guarding.
    """
    rows = await recent_context(agent, limit=limit)
    if not rows:
        return ""
    lines = [f"Working memory for {agent} (most recent first):"]
    for r in rows:
        # Local date and time: a date alone (in UTC) once let a brief call the
        # previous evening's brief "this morning's".
        when = (r["created_at"].astimezone(ZoneInfo(settings.timezone)).strftime("%b %d %H:%M")
                if r.get("created_at") else "?")
        lines.append(f"  [{when}] {r['summary']}")
    return "\n".join(lines)


__all__ = ["record_context", "recent_context", "recent_context_block"]
