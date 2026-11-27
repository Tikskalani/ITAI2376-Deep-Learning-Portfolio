"""Atlas -- discovery & learning queue data access.

Two tables back the news_discovery learning loop (migration 008):

  discovery_sources    trusted feeds (YouTube channels) the nightly scan sweeps
  learning_candidates  vetted, sales-stripped learnings awaiting Trilok's review

This module is the only place that reads or writes them. The agent and
the (coming) nightly scan call these helpers; judgment lives in the
agent, persistence lives here. Mirrors core/lessons.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core import db

# Statuses a candidate can hold, in lifecycle order.
STATUSES = ("proposed", "pursuing", "drafted", "adopted", "rejected")
_REVIEWED_ON = {"pursuing", "drafted", "adopted", "rejected"}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


async def add_source(
    handle: str,
    kind: str = "youtube_channel",
    channel_id: str | None = None,
    label: str | None = None,
) -> UUID:
    """Add (or revive) a trusted source. Upserts on (kind, handle)."""
    row = await db.fetchrow(
        """
        insert into discovery_sources (kind, handle, channel_id, label, active)
        values ($1, $2, $3, $4, true)
        on conflict (kind, handle) do update
            set channel_id = coalesce(excluded.channel_id, discovery_sources.channel_id),
                label      = coalesce(excluded.label, discovery_sources.label),
                active     = true
        returning id
        """,
        kind, handle, channel_id, label,
    )
    return row["id"]


async def list_sources(active_only: bool = True) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select id, kind, handle, channel_id, label, active, last_scanned_at, created_at
        from discovery_sources
        where (not $1) or active
        order by created_at
        """,
        active_only,
    )
    return [dict(r) for r in rows]


async def set_source_channel_id(source_id: UUID, channel_id: str) -> None:
    await db.execute(
        "update discovery_sources set channel_id = $2 where id = $1",
        source_id, channel_id,
    )


async def deactivate_source(source_id: UUID) -> None:
    await db.execute(
        "update discovery_sources set active = false where id = $1",
        source_id,
    )


async def touch_source_scanned(
    source_id: UUID, when: datetime | None = None
) -> None:
    """Record that we just swept this source."""
    await db.execute(
        "update discovery_sources set last_scanned_at = $2 where id = $1",
        source_id, when or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Learning candidates
# ---------------------------------------------------------------------------


async def create_candidate(
    *,
    learning: str,
    source_type: str,
    source_url: str | None = None,
    creator: str | None = None,
    title: str | None = None,
    novelty: float | None = None,
    relevance: float | None = None,
    credibility: float | None = None,
    verdict: str | None = None,
    rationale: str | None = None,
    dedup_key: str | None = None,
    source_id: UUID | None = None,
) -> UUID:
    """Queue a vetted learning for Trilok's review (status 'proposed')."""
    row = await db.fetchrow(
        """
        insert into learning_candidates
            (learning, source_type, source_url, creator, title,
             novelty, relevance, credibility, verdict, rationale,
             dedup_key, source_id, status)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'proposed')
        returning id
        """,
        learning, source_type, source_url, creator, title,
        novelty, relevance, credibility, verdict, rationale,
        dedup_key, source_id,
    )
    return row["id"]


async def candidate_exists(dedup_key: str) -> bool:
    """Soft-dedup: has a candidate with this key already been seen?"""
    val = await db.fetchval(
        "select exists(select 1 from learning_candidates where dedup_key = $1)",
        dedup_key,
    )
    return bool(val)


async def list_candidates(
    status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    cols = (
        "id, source_type, source_url, creator, title, learning, "
        "novelty, relevance, credibility, verdict, rationale, "
        "status, created_at, reviewed_at"
    )
    if status:
        rows = await db.fetch(
            f"select {cols} from learning_candidates "
            "where status = $1 order by created_at desc limit $2",
            status, limit,
        )
    else:
        rows = await db.fetch(
            f"select {cols} from learning_candidates "
            "order by created_at desc limit $1",
            limit,
        )
    return [dict(r) for r in rows]


async def set_candidate_status(candidate_id: UUID, status: str) -> None:
    """Move a candidate along its lifecycle. Stamps reviewed_at once it leaves 'proposed'."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    reviewed = datetime.now(UTC) if status in _REVIEWED_ON else None
    await db.execute(
        """
        update learning_candidates
        set status = $2,
            reviewed_at = coalesce($3, reviewed_at)
        where id = $1
        """,
        candidate_id, status, reviewed,
    )


_CANDIDATE_COLS = (
    "id, source_type, source_url, creator, title, learning, "
    "novelty, relevance, credibility, verdict, rationale, "
    "status, created_at, reviewed_at"
)


async def list_undigested(limit: int = 20) -> list[dict[str, Any]]:
    """Proposed candidates not yet sent in a digest, newest first."""
    limit = max(1, min(int(limit or 20), 200))
    rows = await db.fetch(
        f"select {_CANDIDATE_COLS} from learning_candidates "
        "where status = 'proposed' and digested_at is null "
        "order by created_at desc limit $1",
        limit,
    )
    return [dict(r) for r in rows]


async def mark_digested(candidate_ids: list[UUID]) -> None:
    """Stamp candidates as sent in a digest so they are not re-sent."""
    if not candidate_ids:
        return
    await db.execute(
        "update learning_candidates set digested_at = now() where id = any($1::uuid[])",
        list(candidate_ids),
    )


async def transition(
    candidate_id: UUID, to_status: str, *, from_statuses: tuple[str, ...]
) -> bool:
    """Guarded status move: only transitions if the candidate is currently in
    one of from_statuses. Returns True if it moved. Use this for the Telegram
    taps so a stale card cannot drag an already-actioned candidate backwards."""
    if to_status not in STATUSES:
        raise ValueError(f"unknown status {to_status!r}")
    reviewed = datetime.now(UTC) if to_status in _REVIEWED_ON else None
    row = await db.fetchrow(
        """
        update learning_candidates
        set status = $2, reviewed_at = coalesce($3, reviewed_at)
        where id = $1 and status = any($4::text[])
        returning id
        """,
        candidate_id, to_status, reviewed, list(from_statuses),
    )
    return row is not None


async def weekly_summary(days: int = 7) -> dict[str, Any]:
    """Funnel + wins for the weekly review: candidate counts by status over
    the window, the learnings adopted in the window, and the total still
    awaiting review."""
    by_status = await db.fetch(
        "select status, count(*) as c from learning_candidates "
        "where created_at >= now() - make_interval(days => $1) group by status",
        days,
    )
    adopted = await db.fetch(
        "select learning, creator, source_url from learning_candidates "
        "where status = 'adopted' and reviewed_at >= now() - make_interval(days => $1) "
        "order by reviewed_at desc limit 25",
        days,
    )
    pending = await db.fetchval(
        "select count(*) from learning_candidates where status = 'proposed'"
    )
    return {
        "days": days,
        "by_status": {r["status"]: r["c"] for r in by_status},
        "adopted": [dict(r) for r in adopted],
        "pending_total": int(pending or 0),
    }


async def list_review_queue(limit: int = 20) -> list[dict[str, Any]]:
    """The active queue the dashboard acts on: proposed + drafted candidates,
    drafted first. Includes the stored CTO draft (from metadata) so the UI can
    show it inline with an Adopt button."""
    limit = max(1, min(int(limit or 20), 100))
    rows = await db.fetch(
        f"""
        select {_CANDIDATE_COLS}, metadata
        from learning_candidates
        where status in ('proposed', 'drafted')
        order by (status = 'drafted') desc, created_at desc
        limit $1
        """,
        limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        meta = d.pop("metadata", None) or {}
        d["draft"] = meta.get("draft")
        out.append(d)
    return out


async def is_seen(video_id: str) -> bool:
    """Has the discovery scan already vetted this video (kept or skipped)?"""
    return bool(await db.fetchval(
        "select exists(select 1 from seen_videos where video_id = $1)", video_id
    ))


async def mark_seen(video_id: str, source_id: UUID | None = None) -> None:
    """Record a video as vetted so the next sweep does not re-process it."""
    await db.execute(
        "insert into seen_videos (video_id, source_id) values ($1, $2) "
        "on conflict (video_id) do nothing",
        video_id, source_id,
    )


async def get_candidate(candidate_id: UUID) -> dict[str, Any] | None:
    """Load one candidate with everything, including metadata (where the draft lives)."""
    row = await db.fetchrow(
        """
        select id, source_type, source_url, creator, title, learning,
               novelty, relevance, credibility, verdict, rationale,
               status, dedup_key, source_id, metadata, created_at, reviewed_at
        from learning_candidates
        where id = $1
        """,
        candidate_id,
    )
    return dict(row) if row else None


async def store_draft(candidate_id: UUID, draft: dict[str, Any]) -> None:
    """Attach a CTO draft to a candidate and move it to 'drafted'."""
    await db.execute(
        """
        update learning_candidates
        set metadata = coalesce(metadata, '{}'::jsonb) || $2::jsonb,
            status = 'drafted'
        where id = $1
        """,
        candidate_id,
        {"draft": draft},
    )


__all__ = [
    "STATUSES",
    "add_source", "list_sources", "set_source_channel_id",
    "deactivate_source", "touch_source_scanned",
    "create_candidate", "candidate_exists", "list_candidates",
    "set_candidate_status", "get_candidate", "store_draft",
    "list_undigested", "mark_digested", "transition",
    "is_seen", "mark_seen", "weekly_summary", "list_review_queue",
]
