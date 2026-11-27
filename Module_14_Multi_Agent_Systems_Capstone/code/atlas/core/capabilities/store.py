"""Database access for capabilities (migration 038). Judgment lives in the
pipeline; persistence lives here."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from core import db

DONE_OR_RUNNING = ("processing", "done", "held_budget")


def _row(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    out = dict(r)
    for k, v in out.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k in ("cost_usd", "cap_usd", "start_s", "end_s") and v is not None:
            out[k] = float(v)
    return out


# ------------------------------------------------------------------ sources


async def get_source_by_canonical(canonical: str) -> dict[str, Any] | None:
    return _row(await db.fetchrow("select * from cap_sources where canonical = $1", canonical))


async def get_source(source_id: UUID | str) -> dict[str, Any] | None:
    return _row(await db.fetchrow("select * from cap_sources where id = $1", UUID(str(source_id))))


async def create_source(url: str, canonical: str, kind: str, cap_usd: float) -> dict[str, Any]:
    row = await db.fetchrow(
        "insert into cap_sources (url, canonical, kind, cap_usd) values ($1, $2, $3, $4) "
        "on conflict (canonical) do update set updated_at = cap_sources.updated_at returning *",
        url, canonical, kind, cap_usd,
    )
    out = _row(row)
    assert out is not None
    return out


async def claim_source(source_id: UUID | str, reason: str) -> UUID | None:
    """Move a source to processing and open an attempt, unless another run
    holds it. Returns the attempt id, or None when it is already running."""
    # One statement, so two runs can never both claim the same source.
    aid = await db.fetchval(
        """
        with s as (
          update cap_sources set status = 'processing', stage = 'capture', error = null,
                 attempts = attempts + 1, updated_at = now()
           where id = $1 and status <> 'processing' returning id)
        insert into cap_attempts (source_id, reason) select id, $2 from s returning id
        """,
        UUID(str(source_id)), reason,
    )
    return UUID(str(aid)) if aid else None


async def set_stage(source_id: UUID | str, attempt_id: UUID, stage: str) -> None:
    await db.execute("update cap_sources set stage = $2, updated_at = now() where id = $1", UUID(str(source_id)), stage)
    await db.execute("update cap_attempts set stage = $2 where id = $1", attempt_id, stage)


async def finish(source_id: UUID | str, attempt_id: UUID, status: str, error: str | None = None) -> None:
    """status: done, failed or held_budget (the one hold status, everywhere)."""
    await db.execute(
        "update cap_sources set status = $2, error = $3, updated_at = now() where id = $1",
        UUID(str(source_id)), status, error,
    )
    await db.execute(
        "update cap_attempts set status = $2, error = $3, ended_at = now() where id = $1", attempt_id, status, error)


async def set_meta(source_id: UUID | str, title: str | None, author: str | None) -> None:
    await db.execute("update cap_sources set title = coalesce($2, title), author = coalesce($3, author), "
                     "updated_at = now() where id = $1", UUID(str(source_id)), title, author)


async def add_cost(source_id: UUID | str, attempt_id: UUID, usd: float) -> None:
    await db.execute("update cap_sources set cost_usd = cost_usd + $2 where id = $1", UUID(str(source_id)), usd)
    await db.execute("update cap_attempts set cost_usd = cost_usd + $2 where id = $1", attempt_id, usd)


async def list_sources(limit: int = 30) -> list[dict[str, Any]]:
    rows = await db.fetch("select * from cap_sources order by created_at desc limit $1", limit)
    return [r for r in (_row(x) for x in rows) if r]


# ------------------------------------------------------------------ evidence, claims, checks


async def add_evidence(source_id: UUID | str, attempt_id: UUID, ref: str, kind: str, *, text: str | None = None,
                       start_s: float | None = None, end_s: float | None = None, file: str | None = None,
                       url: str | None = None, extraction: str = "exact", meta: dict[str, Any] | None = None) -> None:
    await db.execute(
        "insert into cap_evidence (source_id, attempt_id, ref, kind, text, start_s, end_s, file, url, extraction, meta) "
        "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        UUID(str(source_id)), attempt_id, ref, kind, text, start_s, end_s, file, url, extraction, meta or {},
    )


async def evidence(attempt_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch("select * from cap_evidence where attempt_id = $1 order by created_at, ref", attempt_id)
    return [r for r in (_row(x) for x in rows) if r]


async def add_claim(source_id: UUID | str, attempt_id: UUID, text: str, quote: str | None, refs: list[str],
                    fidelity: str, consequential: bool) -> UUID:
    return UUID(str(await db.fetchval(
        "insert into cap_claims (source_id, attempt_id, text, quote, evidence_refs, fidelity, consequential) "
        "values ($1, $2, $3, $4, $5, $6, $7) returning id",
        UUID(str(source_id)), attempt_id, text, quote, refs, fidelity, consequential,
    )))


async def add_check(claim_id: UUID, verdict: str, method: str, url: str | None = None, excerpt: str | None = None) -> None:
    await db.execute("insert into cap_checks (claim_id, verdict, method, checked_url, excerpt) values ($1, $2, $3, $4, $5)",
                     claim_id, verdict, method, url, excerpt)


async def claims(attempt_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select c.*, k.verdict, k.checked_url, k.excerpt, k.method, k.checked_at
          from cap_claims c
          left join lateral (select * from cap_checks where claim_id = c.id order by checked_at desc limit 1) k on true
         where c.attempt_id = $1 order by c.created_at
        """, attempt_id)
    return [r for r in (_row(x) for x in rows) if r]


# ------------------------------------------------------------------ packages and versions


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "procedure"


async def new_version(*, title: str, source_id: UUID | str, attempt_id: UUID, body: dict[str, Any],
                      check_status: str, complete: bool, project_slug: str | None = None) -> dict[str, Any]:
    """A new immutable version under the package for this source (one package
    per source). Only the run holding the source writes here (claim_source)."""
    pkg = await db.fetchrow(
        "select p.id from cap_packages p join cap_versions v on v.package_id = p.id "
        "where v.source_id = $1 limit 1", UUID(str(source_id)))
    if pkg is None:
        base, n = slugify(title), 1
        slug = base
        while await db.fetchval("select 1 from cap_packages where slug = $1", slug):
            n += 1
            slug = f"{base}-{n}"
        pkg = await db.fetchrow(
            "insert into cap_packages (slug, title, project_slug) values ($1, $2, $3) returning id",
            slug, title[:200], project_slug)
    row = await db.fetchrow(
        "insert into cap_versions (package_id, version, source_id, attempt_id, body, check_status, complete) "
        "select $1, coalesce(max(version), 0) + 1, $2, $3, $4, $5, $6 from cap_versions where package_id = $1 "
        "returning *",
        pkg["id"], UUID(str(source_id)), attempt_id, json.loads(json.dumps(body)), check_status, complete)
    out = _row(row)
    assert out is not None
    return out


# Approved = the latest review is "approved". Withdrawn or never reviewed = not usable.
_STATE = """
    (select decision from cap_reviews r where r.version_id = v.id order by r.created_at desc limit 1)
"""


async def version(version_id: UUID | str) -> dict[str, Any] | None:
    try:
        vid = UUID(str(version_id))
    except ValueError:
        return None
    row = await db.fetchrow(
        f"select v.*, p.slug, p.title, p.project_slug, {_STATE} as review, s.url as source_url, "
        "s.title as source_title, s.author as source_author "
        "from cap_versions v join cap_packages p on p.id = v.package_id "
        "left join cap_sources s on s.id = v.source_id where v.id = $1", vid)
    return _row(row)


async def review(version_id: UUID | str, decision: str, note: str | None = None) -> None:
    if decision not in ("approved", "withdrawn"):
        raise ValueError("decision is approved or withdrawn")
    await db.execute("insert into cap_reviews (version_id, decision, note) values ($1, $2, $3)",
                     UUID(str(version_id)), decision, note)


async def list_versions(limit: int = 40) -> list[dict[str, Any]]:
    rows = await db.fetch(
        f"select v.id, v.version, v.check_status, v.test_status, v.complete, v.created_at, v.source_id, "
        f"p.slug, p.title, p.project_slug, {_STATE} as review, "
        "(select count(*) from cap_uses u where u.version_id = v.id) as uses, "
        "(select count(*) from cap_exports e where e.version_id = v.id and e.removed_at is null) as exported "
        "from cap_versions v join cap_packages p on p.id = v.package_id order by v.created_at desc limit $1", limit)
    return [r for r in (_row(x) for x in rows) if r]


__all__ = [
    "add_check", "add_claim", "add_cost", "add_evidence", "claim_source", "claims", "create_source", "evidence",
    "finish", "get_source", "get_source_by_canonical", "list_sources", "list_versions", "new_version", "review",
    "set_meta", "set_stage", "slugify", "version",
]
