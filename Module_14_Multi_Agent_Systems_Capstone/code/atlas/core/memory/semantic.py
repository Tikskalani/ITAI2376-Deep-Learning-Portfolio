"""Atlas — semantic memory.

Entities, attributes, relationships, and embedded facts. The shape mirrors
the schema in db/migrations/001_initial.sql. Facts are embedded at write
time using core.memory.embeddings.embed; search_facts embeds the query and
runs a cosine-distance HNSW lookup.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core import db
from core.memory.embeddings import embed

# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


async def add_entity(
    type: str,
    name: str,
    canonical_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Insert an entity, return its UUID."""
    row = await db.fetchrow(
        """
        insert into entities (type, name, canonical_name, metadata)
        values ($1, $2, $3, $4)
        returning id
        """,
        type,
        name,
        canonical_name,
        metadata or {},
    )
    return row["id"]


async def get_entity(entity_id: UUID) -> dict[str, Any] | None:
    """Return the entity plus its attributes and relationships, or None."""
    ent = await db.fetchrow(
        "select id, type, name, canonical_name, metadata, created_at, updated_at "
        "from entities where id = $1",
        entity_id,
    )
    if ent is None:
        return None

    attrs = await db.fetch(
        "select key, value, source, confidence, created_at "
        "from entity_attributes where entity_id = $1 order by created_at",
        entity_id,
    )
    outgoing = await db.fetch(
        "select id, to_entity, rel_type, metadata, created_at "
        "from relationships where from_entity = $1",
        entity_id,
    )
    incoming = await db.fetch(
        "select id, from_entity, rel_type, metadata, created_at "
        "from relationships where to_entity = $1",
        entity_id,
    )
    return {
        "id": ent["id"],
        "type": ent["type"],
        "name": ent["name"],
        "canonical_name": ent["canonical_name"],
        "metadata": ent["metadata"],
        "created_at": ent["created_at"],
        "updated_at": ent["updated_at"],
        "attributes": [dict(a) for a in attrs],
        "relationships": {
            "outgoing": [dict(r) for r in outgoing],
            "incoming": [dict(r) for r in incoming],
        },
    }


async def find_entities_by_name(name: str) -> list[dict[str, Any]]:
    """Case-insensitive lookup on name and canonical_name. Exact matches only."""
    rows = await db.fetch(
        """
        select id, type, name, canonical_name, metadata, created_at, updated_at
        from entities
        where lower(name) = lower($1) or lower(canonical_name) = lower($1)
        order by created_at desc
        """,
        name,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


async def add_attribute(
    entity_id: UUID,
    key: str,
    value: str,
    source: str | None = None,
    confidence: float = 1.0,
) -> UUID:
    row = await db.fetchrow(
        """
        insert into entity_attributes (entity_id, key, value, source, confidence)
        values ($1, $2, $3, $4, $5)
        returning id
        """,
        entity_id,
        key,
        value,
        source,
        confidence,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


async def add_relationship(
    from_entity: UUID,
    to_entity: UUID,
    rel_type: str,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    row = await db.fetchrow(
        """
        insert into relationships (from_entity, to_entity, rel_type, metadata)
        values ($1, $2, $3, $4)
        returning id
        """,
        from_entity,
        to_entity,
        rel_type,
        metadata or {},
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


async def add_fact(
    content: str,
    entity_ids: list[UUID] | None = None,
    source: str | None = None,
    confidence: float = 1.0,
) -> UUID:
    """Embed `content` and store it as a fact."""
    vec = await embed(content)
    row = await db.fetchrow(
        """
        insert into facts (content, embedding, entity_ids, source, confidence)
        values ($1, $2, $3, $4, $5)
        returning id
        """,
        content,
        vec,
        entity_ids or [],
        source,
        confidence,
    )
    return row["id"]


async def search_facts(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Hybrid search (vector + lexical, reciprocal rank fusion) over facts.

    Also bumps last_accessed on the returned rows so the consolidation
    job can decay the long tail that nobody reads.
    """
    from core.memory import retrieval

    rows = await retrieval.search("facts", query, limit=limit)
    if rows:
        ids = [r["id"] for r in rows]
        await db.execute(
            "update facts set last_accessed = now() where id = any($1::uuid[])",
            ids,
        )
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "entity_ids": list(r["entity_ids"] or []),
            "source": r["source"],
            "confidence": r["confidence"],
            "project_id": r.get("project_id"),
            "created_at": r["created_at"],
            "last_accessed": r["last_accessed"],
            "score": r["score"],
            "matched": r["matched"],
        }
        for r in rows
    ]
