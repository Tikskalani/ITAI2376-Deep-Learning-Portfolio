"""Atlas -- the lessons store.

Distilled "what we learned" statements, embedded for retrieval. A lesson
is the durable takeaway from an episode that went well or badly, the kind
of thing that should shape how an agent acts next time. Where facts are
what Atlas knows, lessons are what Atlas has learned.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core import db
from core.memory.embeddings import embed


async def record_lesson(
    content: str,
    domain: str | None = None,
    source_episode: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Embed `content` and store it as a lesson. Returns its id."""
    vec = await embed(content)
    row = await db.fetchrow(
        """
        insert into lessons
          (content, domain, source_episode, embedding, metadata)
        values ($1, $2, $3, $4, $5)
        returning id
        """,
        content,
        domain,
        source_episode,
        vec,
        metadata or {},
    )
    return row["id"]


async def search_lessons(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Hybrid search (vector + lexical + recency, RRF) over lessons."""
    from core.memory import retrieval

    rows = await retrieval.search("lessons", query, limit=limit)
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "domain": r["domain"],
            "source_episode": r["source_episode"],
            "project_id": r.get("project_id"),
            "created_at": r["created_at"],
            "score": r["score"],
            "matched": r["matched"],
        }
        for r in rows
    ]


async def recent_lessons(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recently recorded lessons, newest first."""
    rows = await db.fetch(
        """
        select id, content, domain, source_episode, created_at
        from lessons
        order by created_at desc
        limit $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


__all__ = ["record_lesson", "search_lessons", "recent_lessons"]
