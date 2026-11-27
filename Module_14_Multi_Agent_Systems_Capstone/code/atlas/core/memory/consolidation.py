"""Atlas -- nightly memory consolidation ("sleep").

Four passes, each guarded, run at 02:30 by the scheduler:

  1. dedupe      facts whose embeddings are near-identical (cosine
                 similarity above 0.95) collapse to the oldest; the
                 duplicate's provenance is noted on the survivor
  2. decay       confidence on facts nobody has read in 90 days drops
                 by a tenth each night, floored at 0.2, so stale claims
                 rank below fresh ones without being deleted
  3. expire      profile constraints past their date are removed
  4. context packs  one Haiku call per active project writes a 300 to
                 800 token pack: objective, state, open items, people,
                 the last signals, relevant lessons. The CEO loads it for
                 a brief; MCP serves it to a Claude session that says
                 "working on Kirkdale". Counted against the cap through
                 llm.complete.

Returns a summary dict for the heartbeat. Never raises.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core import db, llm, privacy
from core.memory import retrieval
from core.profile import expire_notes
from core.projects import Project, list_projects
from core.signals import list_signals

log = logging.getLogger(__name__)

_DUPLICATE_SIMILARITY = 0.95
_STALE_DAYS = 90
_DECAY = 0.9
_FLOOR = 0.2

_PACK_SYSTEM = """You write the context pack for one of Trilok Kalani's projects: the 300 to 800
token briefing a Claude session or Atlas's CEO reads before working on it. Plain prose
and short lines, no markdown headers, no bullets longer than a line. Cover, in this
order: objective and current state in two sentences; open items and who they wait on;
hard dates; key people and how to reach them if known; what arrived recently (from the
signals) and what it changed; lessons that apply. Use only the material given; where
something is unknown say so in four words or fewer rather than guessing. No em-dashes."""


async def dedupe_facts(*, similarity: float = _DUPLICATE_SIMILARITY, limit: int = 200) -> int:
    """Collapse near-duplicate facts. Returns how many rows were removed."""
    rows = await db.fetch(
        """
        select a.id as keep, b.id as drop, b.source as drop_source
        from facts a
        join facts b on b.id <> a.id
          and b.created_at > a.created_at
          and a.embedding is not null and b.embedding is not null
          and 1 - (a.embedding <=> b.embedding) >= $1
        order by a.created_at
        limit $2
        """,
        similarity,
        limit,
    )
    dropped: set[Any] = set()
    for r in rows:
        if r["drop"] in dropped or r["keep"] in dropped:
            continue
        await db.execute(
            """
            update facts set confidence = least(1.0, confidence + 0.05),
                             source = case when source like '%+dup%' then source
                                           else coalesce(source, '') || ' +dup(' || coalesce($2, '?') || ')' end
            where id = $1
            """,
            r["keep"],
            r["drop_source"],
        )
        await db.execute("delete from facts where id = $1", r["drop"])
        dropped.add(r["drop"])
    if dropped:
        log.info("consolidation: removed %d duplicate fact(s)", len(dropped))
    return len(dropped)


async def decay_stale_facts(*, days: int = _STALE_DAYS) -> int:
    rows = await db.fetch(
        """
        update facts
        set confidence = greatest($3, confidence * $2)
        where last_accessed < now() - ($1 * interval '1 day') and confidence > $3
        returning id
        """,
        days,
        _DECAY,
        _FLOOR,
    )
    if rows:
        log.info("consolidation: decayed %d stale fact(s)", len(rows))
    return len(rows)


async def _pack_material(project: Project) -> str:
    parts = [
        f"Project: {project.name} ({project.slug}), kind {project.kind}, priority {project.priority}, status {project.status}.",
        f"Objective: {project.objective or 'not stated'}",
        f"Next action: {project.next_action or 'none recorded'}"
        + (f", due {project.next_action_due.isoformat()}" if project.next_action_due else ""),
    ]
    deadlines = await db.fetch(
        "select title, due_date, priority from deadlines where project_id = $1 and status = 'open' "
        "order by due_date limit 10",
        project.id,
    )
    if deadlines:
        parts.append("Open deadlines:\n" + "\n".join(
            f"- {d['due_date'].isoformat()} {d['title']} ({d['priority']})" for d in deadlines))
    sigs = privacy.visible(await list_signals(None, limit=12, project_id=project.id))  # privacy gate
    sigs = sorted(sigs, key=lambda s: s["occurred_at"], reverse=True)[:10]
    if sigs:
        parts.append("Recent signals (newest first):\n" + "\n".join(
            f"- {s['occurred_at'].astimezone(UTC).strftime('%m-%d')} [{s['source']}] "
            f"{s.get('triage_notes') or s['subject']}" for s in sigs))
    try:
        facts = await retrieval.search("facts", f"{project.name} {project.slug}", limit=8)
        if facts:
            parts.append("Facts on record:\n" + "\n".join(f"- {f['content']}" for f in facts))
    except Exception:
        log.exception("consolidation: fact search failed for %s", project.slug)
    try:
        lessons = await retrieval.search("lessons", f"{project.name} {project.kind}", limit=4)
        if lessons:
            parts.append("Lessons that may apply:\n" + "\n".join(f"- {le['content']}" for le in lessons))
    except Exception:
        log.exception("consolidation: lesson search failed for %s", project.slug)
    return "\n\n".join(parts)


async def regenerate_context_packs(*, projects: list[Project] | None = None) -> int:
    """One Haiku call per active project. Returns how many packs were written."""
    projects = projects if projects is not None else await list_projects("active")
    written = 0
    for p in projects:
        try:
            material = await _pack_material(p)
            text = await llm.complete(
                system=_PACK_SYSTEM, user=material, tier="scan", max_tokens=900,
                purpose="context_pack", cache_system=True,
            )
            text = (text or "").strip()
            if not text:
                log.warning("consolidation: empty context pack for %s; keeping the old one", p.slug)
                continue
            await db.execute(
                "update projects set context_pack = $2, context_pack_at = now(), updated_at = now() where id = $1",
                p.id, text[:6000],
            )
            written += 1
        except Exception:
            log.exception("consolidation: context pack failed for %s", p.slug)
    return written


async def run_consolidation() -> dict[str, Any]:
    """Scheduler entry. Never raises."""
    out: dict[str, Any] = {"duplicates_removed": 0, "facts_decayed": 0, "notes_expired": 0, "packs_written": 0}
    for key, fn in (
        ("duplicates_removed", dedupe_facts),
        ("facts_decayed", decay_stale_facts),
        ("notes_expired", expire_notes),
        ("packs_written", regenerate_context_packs),
    ):
        try:
            out[key] = await fn()
        except Exception:
            log.exception("consolidation: %s failed", key)
    log.info("consolidation done: %s", out)
    try:
        from core.heartbeats import set_heartbeat

        await set_heartbeat("memory/consolidation", {**out, "at": datetime.now(UTC).isoformat()})
    except Exception:
        log.exception("consolidation: could not write heartbeat")
    return out


__all__ = ["dedupe_facts", "decay_stale_facts", "regenerate_context_packs", "run_consolidation"]
