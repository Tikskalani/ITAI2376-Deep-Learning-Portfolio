"""Applications, events and postings in Postgres.

An application is keyed by company and role, normalized, so the same job
reached from a Gmail confirmation, a discovered posting and a manual entry
is one row. Status only moves forward (applied, screening, interview,
offer) except for the terminal outcomes; an event records every move.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from core import db

STATUSES = ("interested", "applied", "screening", "interview", "offer", "rejected", "withdrawn", "ghosted")
OPEN = ("interested", "applied", "screening", "interview", "offer")
# Forward order. A terminal status (rejected, withdrawn) replaces any open
# one; a later reply revives a "ghosted" application.
_RANK = {"interested": 0, "applied": 1, "ghosted": 1, "screening": 2, "interview": 3, "offer": 4}
TERMINAL = ("rejected", "withdrawn")

_LEGAL = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|the)\b\.?", re.I)


def norm(text: str) -> str:
    text = _LEGAL.sub(" ", (text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def app_key(company: str, role: str) -> str:
    return f"{norm(company)}|{norm(role)}"


def next_status(current: str | None, proposed: str) -> str:
    """The status after a proposed move. Never moves an open application
    backwards; terminal outcomes stick unless an offer arrives."""
    if proposed not in STATUSES:
        return current or "applied"
    if current is None:
        return proposed
    if current == proposed:
        return current
    if proposed in TERMINAL:
        return proposed
    if current in TERMINAL:
        return proposed if proposed == "offer" else current
    if proposed == "ghosted":
        return "ghosted" if current in ("applied", "screening") else current
    return proposed if _RANK.get(proposed, 0) >= _RANK.get(current, 0) else current


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("id", "posting_id", "application_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    for k, v in list(d.items()):
        if isinstance(v, datetime | date):
            d[k] = v.isoformat()
    return d


async def find_application(company: str, role: str | None = None, thread_id: str | None = None) -> dict[str, Any] | None:
    """By thread, then by exact key, then by company when it has exactly one
    open application (mail often names the company and not the role)."""
    if thread_id:
        r = await db.fetchrow("select * from job_applications where $1 = any(thread_ids) limit 1", thread_id)
        if r:
            return _row(r)
    if role:
        r = await db.fetchrow("select * from job_applications where key = $1", app_key(company, role))
        if r:
            return _row(r)
    rows = await db.fetch(
        "select * from job_applications where split_part(key, '|', 1) = $1 and status = any($2::text[])",
        norm(company),
        list(OPEN),
    )
    return _row(rows[0]) if len(rows) == 1 else None


async def upsert_application(
    company: str,
    role: str,
    *,
    status: str = "applied",
    source: str = "manual",
    url: str | None = None,
    applied_at: datetime | None = None,
    contact: str | None = None,
    notes: str | None = None,
    posting_id: UUID | str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Create the application or move the existing one. Returns the row."""
    key = app_key(company, role)
    existing = await db.fetchrow("select * from job_applications where key = $1", key)
    if existing is None:
        r = await db.fetchrow(
            """
            insert into job_applications
              (key, company, role, url, source, status, applied_at, contact, notes, posting_id, thread_ids)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            on conflict (key) do update set updated_at = now()
            returning *
            """,
            key, company.strip(), role.strip(), url, source, status,
            applied_at or (datetime.now(UTC) if status != "interested" else None),
            contact, notes, UUID(str(posting_id)) if posting_id else None,
            [thread_id] if thread_id else [],
        )
        return _row(r)
    new = next_status(existing["status"], status)
    r = await db.fetchrow(
        """
        update job_applications set
          status = $2,
          url = coalesce(url, $3),
          contact = coalesce($4, contact),
          applied_at = coalesce(applied_at, $5),
          thread_ids = case when $6::text is null or $6 = any(thread_ids) then thread_ids
                            else array_append(thread_ids, $6) end,
          updated_at = now()
        where id = $1
        returning *
        """,
        existing["id"], new, url, contact, applied_at, thread_id,
    )
    return _row(r)


async def record_event(
    application_id: UUID | str,
    kind: str,
    detail: str = "",
    *,
    at: datetime | None = None,
    message_id: str | None = None,
) -> bool:
    """Log an event; False when this Gmail message was already recorded."""
    r = await db.fetchval(
        """
        insert into job_events (application_id, at, kind, detail, message_id)
        values ($1, coalesce($2, now()), $3, $4, $5)
        on conflict (message_id) do nothing
        returning id
        """,
        UUID(str(application_id)), at, kind, detail[:2000], message_id,
    )
    return r is not None


async def message_seen(message_id: str) -> bool:
    return bool(await db.fetchval("select exists (select 1 from job_events where message_id = $1)", message_id))


async def set_status(application_id: UUID | str, status: str, note: str = "") -> dict[str, Any] | None:
    """A manual move (from the agent or the dashboard): no forward-only rule."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    r = await db.fetchrow(
        "update job_applications set status = $2, updated_at = now() where id = $1 returning *",
        UUID(str(application_id)), status,
    )
    if r:
        await record_event(r["id"], f"status:{status}", note)
    return _row(r) if r else None


async def touch_contact(application_id: UUID | str, when: datetime) -> None:
    await db.execute(
        "update job_applications set last_contact_at = greatest(coalesce(last_contact_at, $2), $2), "
        "updated_at = now() where id = $1",
        UUID(str(application_id)), when,
    )


async def set_next_action(application_id: UUID | str, action: str | None, due: datetime | None) -> None:
    await db.execute(
        "update job_applications set next_action = $2, next_action_at = $3, updated_at = now() where id = $1",
        UUID(str(application_id)), action, due,
    )


async def save_materials(application_id: UUID | str, materials: dict[str, Any]) -> None:
    await db.execute(
        "update job_applications set materials = materials || $2::jsonb, updated_at = now() where id = $1",
        UUID(str(application_id)), materials,
    )


async def get_application(application_id: UUID | str) -> dict[str, Any] | None:
    r = await db.fetchrow("select * from job_applications where id = $1", UUID(str(application_id)))
    if not r:
        return None
    out = _row(r)
    events = await db.fetch(
        "select at, kind, detail, message_id is not null as from_mail from job_events "
        "where application_id = $1 order by at desc limit 30", r["id"]
    )
    out["events"] = [_row(e) for e in events]
    return out


async def list_applications(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if status == "open":
        rows = await db.fetch(
            "select * from job_applications where status = any($1::text[]) "
            "order by coalesce(next_action_at, 'infinity'), updated_at desc limit $2",
            list(OPEN), limit,
        )
    elif status:
        rows = await db.fetch(
            "select * from job_applications where status = $1 order by updated_at desc limit $2", status, limit
        )
    else:
        rows = await db.fetch("select * from job_applications order by updated_at desc limit $1", limit)
    return [_row(r) for r in rows]


async def funnel() -> dict[str, int]:
    rows = await db.fetch("select status, count(*) as n from job_applications group by status")
    out = {s: 0 for s in STATUSES}
    out.update({r["status"]: int(r["n"]) for r in rows})
    return out


# ------------------------------------------------------------------ postings


async def upsert_posting(p: dict[str, Any]) -> tuple[UUID, bool]:
    """Insert or refresh a posting. Returns (id, is_new)."""
    r = await db.fetchrow(
        """
        insert into job_postings
          (source, board, external_id, company, title, location, remote, url,
           salary_min, salary_max, posted_at, description)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        on conflict (source, board, external_id) do update set
          title = excluded.title, location = excluded.location, remote = excluded.remote,
          url = excluded.url, salary_min = excluded.salary_min, salary_max = excluded.salary_max,
          last_seen_at = now(),
          status = case when job_postings.status = 'closed' then 'new' else job_postings.status end
        returning id, (xmax = 0) as inserted
        """,
        p["source"], p["board"], str(p["external_id"]), p["company"], p["title"][:300],
        (p.get("location") or "")[:300] or None, p.get("remote"), p.get("url"),
        p.get("salary_min"), p.get("salary_max"), p.get("posted_at"), (p.get("description") or "")[:8000] or None,
    )
    return r["id"], bool(r["inserted"])


async def unscored_postings(limit: int = 40) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select * from job_postings where score is null and status = 'new' order by first_seen_at desc limit $1", limit
    )
    return [_row(r) for r in rows]


async def clear_scores() -> int:
    """Forget the scores of open postings so they are scored again (after the
    profile changes). Returns how many."""
    r = await db.execute("update job_postings set score = null, reason = null where status in ('new', 'shown')")
    try:
        return int(str(r).split()[-1])
    except (ValueError, IndexError):
        return 0


async def score_posting(posting_id: UUID | str, score: int, reason: str) -> None:
    await db.execute(
        "update job_postings set score = $2, reason = $3 where id = $1",
        UUID(str(posting_id)), max(0, min(100, int(score))), reason[:500],
    )


async def close_missing(source: str, board: str, seen_ids: list[str]) -> int:
    """Postings no longer on a board are closed (it answered, they are gone)."""
    r = await db.execute(
        "update job_postings set status = 'closed' where source = $1 and board = $2 "
        "and status in ('new', 'shown') and not (external_id = any($3::text[]))",
        source, board, seen_ids,
    )
    try:
        return int(str(r).split()[-1])
    except (ValueError, IndexError):
        return 0


async def top_postings(min_score: int = 0, limit: int = 10, statuses: tuple[str, ...] = ("new", "shown")) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select * from job_postings where status = any($1::text[]) and score >= $2 "
        "order by score desc, first_seen_at desc limit $3",
        list(statuses), min_score, limit,
    )
    return [_row(r) for r in rows]


async def get_posting(posting_id: UUID | str) -> dict[str, Any] | None:
    r = await db.fetchrow("select * from job_postings where id = $1", UUID(str(posting_id)))
    return _row(r) if r else None


async def set_posting_status(posting_id: UUID | str, status: str) -> None:
    await db.execute("update job_postings set status = $2 where id = $1", UUID(str(posting_id)), status)


async def mark_shown(ids: list[str]) -> None:
    if ids:
        await db.execute(
            "update job_postings set status = 'shown' where id = any($1::uuid[]) and status = 'new'",
            [UUID(i) for i in ids],
        )


async def seed_from_config(cfg: dict[str, Any]) -> int:
    """Load the applications listed in config/jobs.yaml. Idempotent."""
    n = 0
    for a in cfg.get("applications") or []:
        if not a.get("company") or not a.get("role"):
            continue
        key = app_key(a["company"], a["role"])
        if await db.fetchval("select exists (select 1 from job_applications where key = $1)", key):
            continue
        applied = None
        if a.get("applied"):
            applied = datetime.fromisoformat(str(a["applied"])).replace(tzinfo=UTC)
        row = await upsert_application(
            a["company"], a["role"], status=a.get("status", "applied"),
            source=a.get("source", "manual"), url=a.get("url"), applied_at=applied,
        )
        await record_event(row["id"], "applied", "Loaded from config/jobs.yaml", at=applied)
        n += 1
    return n


__all__ = [
    "OPEN", "STATUSES", "TERMINAL", "app_key", "close_missing", "find_application", "funnel",
    "get_application", "get_posting", "list_applications", "mark_shown", "message_seen", "next_status",
    "norm", "record_event", "save_materials", "score_posting", "seed_from_config", "set_next_action",
    "set_posting_status", "set_status", "top_postings", "touch_contact", "unscored_postings",
    "upsert_application", "upsert_posting",
]
