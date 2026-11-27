"""Atlas -- knowledge graph activation.

The entities and relationships tables exist (migration 001) but nothing
writes to them automatically. This module fires a Haiku extractor over
CEO synthesis text, upserts the named entities into Postgres, and
records the typed relationships between them. After a few weeks of
running, "what do I know about Example Property Group" can return a structured
view instead of vector-searched fact fragments.

Design constraints:
  * Haiku only -- cheap tier, pennies per day even at heavy use.
  * Fire and forget -- the user reply is delivered first; extraction
    runs in the background and never blocks or raises into the caller.
  * Idempotent entity writes -- a soft dedup by canonical_name + type
    so repeat mentions of "Example Property Group" don't fan out into
    duplicate rows.
  * Relationships are insert-only for now. Duplicate edges are not
    harmful; cleanup, if ever needed, is a hygiene-job concern.

The Haiku call uses a tight structured-JSON prompt. We never trust the
model's typing; the parser is defensive about missing keys, wrong
types, and JSON wrapped in markdown fences.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from uuid import UUID

from core import db
from core.memory.semantic import add_relationship

log = logging.getLogger(__name__)


# Skip extraction for very short replies -- not worth the Haiku call.
MIN_TEXT_CHARS = 60

# Haiku response budget. Most extractions fit in 200 tokens; 800 is
# generous without being unbounded.
_HAIKU_MAX_TOKENS = 800

# Entities the extractor is allowed to emit. Anything else is dropped
# at parse time so model creativity does not pollute the type column.
ALLOWED_TYPES = {
    "person",
    "business",
    "property",
    "course",
    "account",
    "supplier",
    "product",
    "agency",
    "tool",
    "concept",
}

# Loose, fenced-JSON regex -- some Haiku replies still wrap in ```json
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


_EXTRACTOR_PROMPT = """\
Extract entities and relationships from the text below. Return ONLY \
valid JSON, no markdown fences, no preamble. Schema:

{
  "entities": [
    {"type": "person|business|property|course|account|supplier|product|agency|tool|concept",
     "name": "exact name as it appears",
     "canonical_name": "lowercased, deduplicated form"}
  ],
  "relationships": [
    {"from": "entity name", "to": "entity name", "rel_type": "owns|supplies|manages|employs|enrolled_at|married_to|uses|partners_with"}
  ]
}

Rules:
- Only include entities that are real-world named things. Skip generic \
nouns ("a customer", "the meeting").
- canonical_name is name.lower().strip() unless a clearer canonical \
form exists ("Example Property Group" -> "example property group").
- Both endpoints of a relationship MUST appear in entities.
- If nothing is extractable, return {"entities": [], "relationships": []}.

Text:
"""


def _strip_fences(s: str) -> str:
    """If the model wrapped JSON in ```json ... ```, pull out the inside."""
    m = _JSON_FENCE.search(s)
    return m.group(1).strip() if m else s.strip()


def _parse_extraction(raw: str) -> dict[str, list[dict[str, Any]]]:
    """Parse Haiku's response. Returns {"entities": [...], "relationships": [...]}.

    Defensive: drops anything that isn't a dict with the expected keys,
    filters entity types to ALLOWED_TYPES, and skips relationships whose
    endpoints aren't in the entity list. Never raises.
    """
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        log.warning("knowledge extractor: invalid JSON, dropping run")
        return {"entities": [], "relationships": []}

    if not isinstance(data, dict):
        return {"entities": [], "relationships": []}

    entities: list[dict[str, Any]] = []
    names_seen: set[str] = set()
    for e in data.get("entities") or []:
        if not isinstance(e, dict):
            continue
        t = (e.get("type") or "").strip().lower()
        n = (e.get("name") or "").strip()
        if not t or not n or t not in ALLOWED_TYPES:
            continue
        canonical = (e.get("canonical_name") or n).strip().lower()
        # Within one extraction we want unique canonicals.
        if canonical in names_seen:
            continue
        names_seen.add(canonical)
        entities.append(
            {"type": t, "name": n, "canonical_name": canonical}
        )

    name_index = {e["name"]: e["canonical_name"] for e in entities}
    name_index.update(
        {e["canonical_name"]: e["canonical_name"] for e in entities}
    )

    relationships: list[dict[str, Any]] = []
    for r in data.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        a = (r.get("from") or "").strip()
        b = (r.get("to") or "").strip()
        rt = (r.get("rel_type") or "").strip().lower()
        if not (a and b and rt):
            continue
        # Resolve to canonicals; drop if either endpoint isn't an entity.
        a_c = name_index.get(a) or name_index.get(a.lower())
        b_c = name_index.get(b) or name_index.get(b.lower())
        if not a_c or not b_c or a_c == b_c:
            continue
        relationships.append(
            {"from": a_c, "to": b_c, "rel_type": rt}
        )

    return {"entities": entities, "relationships": relationships}


async def _haiku_extract(text: str) -> str:
    """Call the scan model with the extractor prompt, return raw response text.

    Goes through core.llm.complete, which shapes the request for the model
    (Haiku 5.5 takes effort, not temperature: passing temperature to the
    1.x SDK raised a TypeError here and every extraction failed silently)
    and records the call against the daily cap. Imported inline so this
    module stays usable in tests without the Anthropic key in env.
    """
    from core.llm import complete

    return await complete(
        system=(
            "You are a precise information extractor. Return only the "
            "JSON requested by the user prompt. No preamble, no fences."
        ),
        user=_EXTRACTOR_PROMPT + text,
        tier="scan",
        max_tokens=_HAIKU_MAX_TOKENS,
        temperature=0.0,  # honored only where the model takes it
        purpose="knowledge_graph",
    )


async def upsert_entity(
    type: str,
    name: str,
    canonical_name: str | None = None,
) -> UUID:
    """Insert an entity if no existing row matches on canonical_name.

    Matching is by canonical_name alone, case-insensitive, regardless of
    type. This is intentional: when Haiku calls "Side Shop" a product
    in one extraction and a business in another, both should resolve to
    the SAME row. The first type wins as canonical; subsequent types are
    tracked under metadata.types so the alias history is preserved.

    Returns the id of the new or existing row.
    """
    canonical = (canonical_name or name).strip().lower()
    existing = await db.fetchrow(
        """
        select id, type, metadata from entities
        where lower(canonical_name) = $1 or lower(name) = $1
        order by created_at asc
        limit 1
        """,
        canonical,
    )
    if existing is not None:
        # If a different type was used this time, record it as an alias
        # under metadata.types so we don't lose the signal.
        if existing["type"] != type:
            meta = dict(existing["metadata"] or {})
            seen = set(meta.get("types") or [])
            seen.add(existing["type"])
            seen.add(type)
            meta["types"] = sorted(seen)
            await db.execute(
                "update entities set metadata = $2 where id = $1",
                existing["id"],
                meta,
            )
        return existing["id"]

    row = await db.fetchrow(
        """
        insert into entities (type, name, canonical_name, metadata)
        values ($1, $2, $3, $4)
        returning id
        """,
        type,
        name,
        canonical,
        {},
    )
    return row["id"]


async def extract_from_text(text: str) -> dict[str, list[dict[str, Any]]]:
    """Run the Haiku extractor and return the parsed payload. Never raises."""
    try:
        raw = await _haiku_extract(text)
    except Exception:
        log.exception("knowledge extractor: Haiku call failed")
        return {"entities": [], "relationships": []}
    return _parse_extraction(raw)


async def link_text(text: str, *, source: str | None = None) -> dict[str, Any]:
    """End-to-end: extract from text, upsert entities, insert relationships.

    Never raises. Returns a stats dict so callers can log or surface in a
    brief if they want.
    """
    text = (text or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        return {
            "ok": True,
            "skipped": "text too short",
            "entities_added": 0,
            "relationships_added": 0,
        }

    payload = await extract_from_text(text)
    entities = payload["entities"]
    relationships = payload["relationships"]

    if not entities and not relationships:
        return {
            "ok": True,
            "entities_added": 0,
            "relationships_added": 0,
        }

    # Upsert entities first; track canonical -> uuid for relationship resolve.
    canonical_to_id: dict[str, UUID] = {}
    entities_added = 0
    for e in entities:
        try:
            ent_id = await upsert_entity(
                type=e["type"],
                name=e["name"],
                canonical_name=e["canonical_name"],
            )
            canonical_to_id[e["canonical_name"]] = ent_id
            entities_added += 1
        except Exception:
            log.exception("knowledge graph: failed to upsert entity %r", e)

    relationships_added = 0
    for r in relationships:
        a_id = canonical_to_id.get(r["from"])
        b_id = canonical_to_id.get(r["to"])
        if not a_id or not b_id:
            continue
        try:
            await add_relationship(
                from_entity=a_id,
                to_entity=b_id,
                rel_type=r["rel_type"],
                metadata={"source": source or "knowledge_graph"},
            )
            relationships_added += 1
        except Exception:
            log.exception(
                "knowledge graph: failed to add relationship %r", r
            )

    return {
        "ok": True,
        "entities_added": entities_added,
        "relationships_added": relationships_added,
        "source": source,
    }


def link_text_fire_and_forget(text: str, *, source: str | None = None) -> None:
    """Schedule link_text as a background task. Returns immediately.

    Use this from run_agent's CEO path so extraction never blocks the
    user's reply. Errors are swallowed inside link_text already.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, nothing to schedule onto. Just drop the work.
        return
    loop.create_task(link_text(text, source=source))


async def merge_duplicate_entities() -> dict[str, int]:
    """Find canonical_name collisions and merge into the oldest row.

    Survivor is the oldest row by created_at. Each duplicate's
    relationships and entity_attributes are repointed to the survivor,
    duplicate types are merged into metadata.types, and the duplicate
    is deleted. Returns a stats dict.

    Idempotent. Safe to run on a schedule.
    """
    from collections import defaultdict

    rows = await db.fetch(
        """
        select id, type, name, canonical_name, metadata, created_at
        from entities
        order by lower(canonical_name), created_at asc
        """
    )

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["canonical_name"] or r["name"]).strip().lower()
        groups[key].append(dict(r))

    merged_rows = 0
    merged_groups = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        merged_groups += 1
        survivor = members[0]
        duplicates = members[1:]

        seen_types = set((survivor["metadata"] or {}).get("types") or [])
        seen_types.add(survivor["type"])

        for dup in duplicates:
            seen_types.add(dup["type"])
            seen_types.update((dup["metadata"] or {}).get("types") or [])
            await db.execute(
                "update relationships set from_entity = $2 where from_entity = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "update relationships set to_entity = $2 where to_entity = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "update entity_attributes set entity_id = $2 where entity_id = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "delete from entities where id = $1", dup["id"]
            )
            merged_rows += 1

        new_meta = dict(survivor["metadata"] or {})
        new_meta["types"] = sorted(seen_types)
        await db.execute(
            "update entities set metadata = $2 where id = $1",
            survivor["id"],
            new_meta,
        )

    return {"groups_merged": merged_groups, "rows_merged": merged_rows}


__all__ = [
    "ALLOWED_TYPES",
    "MIN_TEXT_CHARS",
    "extract_from_text",
    "link_text",
    "link_text_fire_and_forget",
    "upsert_entity",
    "merge_duplicate_entities",
]
