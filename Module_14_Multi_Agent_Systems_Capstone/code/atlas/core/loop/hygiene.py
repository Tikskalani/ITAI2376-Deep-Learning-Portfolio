"""Atlas -- memory hygiene.

A weekly job that keeps the memory layer honest. In Phase 0 it reports
rather than mutates: it surfaces decisions that have come due for review
and counts facts that have gone stale, so curation stays a deliberate,
reviewable act rather than something automatic and destructive. Acting on
what it finds (archiving, deduping) is a later, gated step.

run_hygiene never raises, so a bad pass cannot stop the scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

from core import db
from core.decisions import decisions_due_for_review
from core.knowledge_graph import merge_duplicate_entities

log = logging.getLogger(__name__)

# A fact untouched for this long is a candidate for review.
_STALE_FACT_DAYS = 90


async def run_hygiene() -> dict[str, Any]:
    """One memory-hygiene pass. Never raises. Returns a summary dict."""
    log.info("memory hygiene starting")
    decisions_due = 0
    stale_facts = 0
    kg_stats = {"groups_merged": 0, "rows_merged": 0}

    try:
        from core import events

        pruned = await events.prune(days=30)
        if pruned:
            log.info("hygiene: pruned %d event(s) older than 30 days", pruned)
    except Exception:
        log.exception("hygiene: event prune failed")

    try:
        decisions_due = len(await decisions_due_for_review())
    except Exception:
        log.exception("hygiene: decisions-due check failed")

    try:
        stale_facts = (
            await db.fetchval(
                "select count(*) from facts "
                "where last_accessed < now() - ($1 * interval '1 day')",
                _STALE_FACT_DAYS,
            )
            or 0
        )
    except Exception:
        log.exception("hygiene: stale-fact count failed")

    try:
        kg_stats = await merge_duplicate_entities()
    except Exception:
        log.exception("hygiene: knowledge-graph dedup failed")

    log.info(
        "memory hygiene done: %d decision(s) due for review, "
        "%d stale fact(s), %d entity group(s) merged (%d rows)",
        decisions_due,
        stale_facts,
        kg_stats.get("groups_merged", 0),
        kg_stats.get("rows_merged", 0),
    )
    return {
        "decisions_due": decisions_due,
        "stale_facts": stale_facts,
        "groups_merged": kg_stats.get("groups_merged", 0),
        "rows_merged": kg_stats.get("rows_merged", 0),
    }


__all__ = ["run_hygiene"]
