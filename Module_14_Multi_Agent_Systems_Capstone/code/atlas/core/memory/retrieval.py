"""Atlas -- hybrid retrieval.

Vector search alone misses the things people actually look up: a name, an
invoice number, a course code. Lexical search alone misses paraphrase.
Each lookup here runs both, plus a recency ranking where it matters, and
fuses them with reciprocal rank fusion (RRF): score = sum over lists of
1 / (k + rank). No tuning knobs to drift; a row that is near the top of
two lists beats one that tops a single list.

search(table, query, limit) is generic over the four tables that carry
an embedding and a tsv column (migration 014): facts, episodes, lessons,
signals. The per-table search functions in semantic.py, episodic.py and
lessons.py call it and keep their public shapes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core import db
from core.memory.embeddings import EmbeddingError, embed

log = logging.getLogger(__name__)

_RRF_K = 60
_CANDIDATES = 20

# table -> (columns to return, timestamp column for recency, has recency list)
_TABLES: dict[str, tuple[str, str, bool]] = {
    "facts": ("id, content, entity_ids, source, confidence, project_id, signal_id, episode_id, created_at, last_accessed", "created_at", False),
    "episodes": ("id, started_at, ended_at, agent_name, participants, summary, outcome, what_changed, tags, project_id", "coalesce(ended_at, started_at)", True),
    "lessons": ("id, content, domain, source_episode, project_id, created_at, metadata", "created_at", True),
    "signals": ("id, source, external_id, occurred_at, actor, subject, body, url, project_id, urgency, importance, triage_status, triage_notes, deadline", "occurred_at", True),
}


def rrf(*ranked: list[Any], k: int = _RRF_K) -> list[tuple[Any, float]]:
    """Fuse ranked lists of ids into (id, score) best first."""
    scores: dict[Any, float] = {}
    for lst in ranked:
        for rank, item in enumerate(lst, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


async def search(
    table: str,
    query: str,
    *,
    limit: int = 5,
    candidates: int = _CANDIDATES,
    project_id: Any = None,
) -> list[dict[str, Any]]:
    """Hybrid search over one table. Rows carry 'score' and 'matched'
    (which lists found them: vector, lexical, recent)."""
    if table not in _TABLES:
        raise ValueError(f"no hybrid search for table {table!r}")
    cols, ts_col, use_recency = _TABLES[table]
    query = (query or "").strip()
    if not query:
        return []
    where = "where 1=1" + (" and project_id = $3" if project_id is not None else "")
    extra: list[Any] = [project_id] if project_id is not None else []

    vector_ids: list[Any] = []
    try:
        qvec = await embed(query)
        rows = await db.fetch(
            f"select id from {table} {where} and embedding is not null "
            f"order by embedding <=> $1 limit $2",
            qvec, candidates, *extra,
        )
        vector_ids = [r["id"] for r in rows]
    except EmbeddingError as exc:
        log.warning("hybrid search: embedding unavailable (%s); lexical only", exc)
    except Exception:
        log.exception("hybrid search: vector leg failed on %s", table)

    lexical_ids: list[Any] = []
    try:
        rows = await db.fetch(
            f"select id from {table} {where} and tsv @@ websearch_to_tsquery('english', $1) "
            f"order by ts_rank_cd(tsv, websearch_to_tsquery('english', $1)) desc limit $2",
            query, candidates, *extra,
        )
        lexical_ids = [r["id"] for r in rows]
    except Exception:
        log.exception("hybrid search: lexical leg failed on %s", table)

    recent_ids: list[Any] = []
    if use_recency:
        try:
            rows = await db.fetch(
                f"select id from {table} {where} order by {ts_col} desc nulls last limit $1",
                min(candidates, 10), *extra,
            )
            recent_ids = [r["id"] for r in rows]
        except Exception:
            log.exception("hybrid search: recency leg failed on %s", table)

    fused = rrf(vector_ids, lexical_ids, recent_ids)
    # Recency alone must not surface unrelated rows: keep only ids some
    # relevance list found, unless nothing relevant exists at all.
    relevant = set(vector_ids) | set(lexical_ids)
    ordered = [(i, s) for i, s in fused if i in relevant] or fused[:limit]
    ordered = ordered[:limit]
    if not ordered:
        return []
    ids = [i for i, _ in ordered]
    rows = await db.fetch(f"select {cols} from {table} where id = any($1::uuid[])", ids)
    by_id = {r["id"]: dict(r) for r in rows}
    out: list[dict[str, Any]] = []
    for i, score in ordered:
        row = by_id.get(i)
        if row is None:
            continue
        row["score"] = round(score, 5)
        row["matched"] = [
            name for name, lst in (("vector", vector_ids), ("lexical", lexical_ids), ("recent", recent_ids)) if i in lst
        ]
        out.append(row)
    return out


def age_days(when: datetime | None) -> float:
    if when is None:
        return 0.0
    return max((datetime.now(UTC) - when).total_seconds() / 86400.0, 0.0)


__all__ = ["rrf", "search", "age_days"]
