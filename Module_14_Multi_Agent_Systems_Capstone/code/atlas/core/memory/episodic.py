"""Atlas — episodic memory.

Session narratives. start_episode opens a row with a sentinel summary so
the episode shows up in audit trails even if the agent crashes before
end_episode runs. end_episode embeds the final summary and writes outcome
+ what_changed.

search_episodes blends cosine similarity with a recency bonus so recent
relevant episodes outrank older ones with slightly better text match.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from core import db
from core.memory.embeddings import embed

# Half-life used for the recency weight. An episode 7 days old gets the
# same score as one half as similar but fresh. Tuneable later.
_RECENCY_HALF_LIFE_DAYS = 7.0


async def start_episode(agent_name: str, participants: list[str]) -> UUID:
    """Open an episode. Returns the id immediately; end_episode fills the rest."""
    row = await db.fetchrow(
        """
        insert into episodes (started_at, agent_name, participants, summary)
        values (now(), $1, $2, $3)
        returning id
        """,
        agent_name,
        participants,
        "(in progress)",
    )
    from core import events

    await events.emit("run.started", agent=agent_name, episode_id=row["id"], title="starting")
    return row["id"]


async def end_episode(
    episode_id: UUID,
    summary: str,
    outcome: str,
    what_changed: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    *,
    status: str | None = None,
    result: str | None = None,
) -> None:
    """Close an episode with summary, outcome, optional what_changed and tags.

    status is 'ok', 'incomplete' or 'failed' (countable); result is the
    start of the final answer. The summary is what gets embedded, so callers
    put both the ask and the answer in it."""
    if status is not None and status not in ("ok", "incomplete", "failed"):
        raise ValueError(f"bad episode status {status!r}")
    vec = await embed(summary)
    row = await db.fetchrow(
        """
        update episodes
        set ended_at = now(),
            summary = $2,
            outcome = $3,
            what_changed = $4,
            tags = $5,
            embedding = $6,
            status = coalesce($7, status),
            result = coalesce($8, result)
        where id = $1
        returning agent_name, status
        """,
        episode_id,
        summary,
        outcome,
        what_changed or {},
        tags or [],
        vec,
        status,
        result,
    )
    if row is not None:
        from core import events
        from core.costs import cost_of_usage, model_for_agent

        tokens = (what_changed or {}).get("tokens") or {}
        await events.emit(
            "run.finished", agent=row["agent_name"], episode_id=episode_id, title=summary, detail=outcome,
            data={"status": row["status"], "turns": (what_changed or {}).get("turns"),
                  "cost": str(cost_of_usage(model_for_agent(row["agent_name"]), tokens)) if tokens else None},
        )


async def search_episodes(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Hybrid search over episodes: vector, lexical and recency lists fused
    with reciprocal rank fusion, so a fresh relevant episode wins and an
    exact name or number is found even when the embedding misses it."""
    from core.memory import retrieval

    rows = await retrieval.search("episodes", query, limit=limit)
    return [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "agent_name": r["agent_name"],
            "participants": list(r["participants"] or []),
            "summary": r["summary"],
            "outcome": r["outcome"],
            "what_changed": r["what_changed"],
            "tags": list(r["tags"] or []),
            "project_id": r.get("project_id"),
            "score": r["score"],
            "matched": r["matched"],
        }
        for r in rows
    ]


async def get_recent_episodes(hours: int = 24) -> list[dict[str, Any]]:
    """Return episodes that started within the last `hours` hours, newest first."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows = await db.fetch(
        """
        select id, started_at, ended_at, agent_name, participants,
               summary, outcome, what_changed, tags
        from episodes
        where started_at >= $1
        order by started_at desc
        """,
        cutoff,
    )
    return [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "agent_name": r["agent_name"],
            "participants": list(r["participants"] or []),
            "summary": r["summary"],
            "outcome": r["outcome"],
            "what_changed": r["what_changed"],
            "tags": list(r["tags"] or []),
        }
        for r in rows
    ]
