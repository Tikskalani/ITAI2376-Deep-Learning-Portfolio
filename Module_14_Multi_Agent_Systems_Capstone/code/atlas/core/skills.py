"""Atlas -- skills: adopted learnings that change how agents run.

The learning loop (reel -> transcript -> vetting -> candidate -> Pursue ->
CTO draft -> Adopt) used to end in a procedure file and a lesson, both of
which an agent only saw if it happened to search for them. A skill closes
that gap. It is a short playbook (two to five imperative lines) tied to
the agents it applies to, and the runtime puts it into every run of those
agents as a second cached system block. Each run records which skills it
carried; Trilok's good / bad on an episode flows back onto those skills;
the Sunday review lists the ones in trial with their numbers so he can
keep or retire them. Nothing here fine-tunes a model: Claude performs
better because what Trilok adopted is in front of it every time, and the
system can show whether it helped.

Status: trial (new, counting uses) -> active (kept) | retired (dropped).

Provenance decides what reaches a system prompt, and status only narrows it.
A skill's provenance is where its text came from: user_authored (Trilok
wrote it), agent_reflection (Atlas wrote it from its own runs, whose tool
results can carry untrusted text), external_internet (a video or page), or
unknown. It is set once and never upgraded (a trigger in migration 037
holds it). Injected, while trial or active:
  * user_authored skills;
  * agent_reflection skills whose latest review in skill_reviews is
    "approved". Approval is Trilok's record, kept apart; it does not make
    the text his.
Never injected, approved or not: external_internet and unknown. They stay
listed as reference material, and nothing is deleted.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core import db
from core.config import settings

log = logging.getLogger(__name__)

STATUSES = ("trial", "active", "retired")
PROVENANCES = ("user_authored", "external_internet", "agent_reflection", "unknown")
REVIEW_DECISIONS = ("approved", "revoked")
_BODY_MAX = 1500
_TITLE_MAX = 120
_TTL_SECONDS = 60.0

# Cache of the injectable skills, keyed by agent name. Adopting or retiring
# a skill invalidates it; otherwise an agent run costs no extra query most
# of the time.
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _slugify(value: str, fallback: str = "skill") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (slug or fallback)[:80]


def _agents(applies_to: list[str] | None) -> list[str]:
    names: list[str] = []
    for raw in applies_to or []:
        name = re.sub(r"[^a-z0-9_]+", "", str(raw or "").strip().lower().replace("-", "_"))
        if name and name not in names:
            names.append(name)
    return names or ["all"]


def provenance_for_source_type(source_type: str | None) -> str:
    """The provenance of a skill adopted from a learning candidate. A typed or
    forwarded link ("manual") is still someone else's text, so it is not
    Trilok's: anything not recognised is unknown, never user_authored."""
    return {"reflection": "agent_reflection", "youtube": "external_internet",
            "instagram": "external_internet"}.get((source_type or "").strip().lower(), "unknown")


def injectable(provenance: str | None, review: str | None) -> bool:
    """Whether a live (trial or active) skill may reach a system prompt."""
    return provenance == "user_authored" or (provenance == "agent_reflection" and review == "approved")


def invalidate_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def add_skill(
    title: str,
    body: str,
    *,
    applies_to: list[str] | None = None,
    domain: str | None = None,
    source_candidate_id: UUID | None = None,
    source_url: str | None = None,
    procedure_path: str | None = None,
    status: str = "trial",
    provenance: str = "unknown",
    source_ref: str | None = None,
) -> UUID:
    """Store a skill. The slug is derived from the title and made unique.
    provenance defaults to unknown: a caller must say user_authored."""
    title = (title or "").strip()[:_TITLE_MAX]
    body = (body or "").strip()
    if not title:
        raise ValueError("a skill needs a title")
    if not body or len(body) > _BODY_MAX:
        raise ValueError(f"a skill body must be 1 to {_BODY_MAX} characters")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if provenance not in PROVENANCES:
        raise ValueError(f"unknown provenance {provenance!r}")
    base = _slugify(title)
    slug = base
    n = 2
    while await db.fetchval("select 1 from skills where slug = $1", slug):
        slug = f"{base}-{n}"
        n += 1
    sid = await db.fetchval(
        """
        insert into skills (slug, title, body, domain, applies_to, status,
                            source_candidate_id, source_url, procedure_path,
                            provenance, source_ref)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        returning id
        """,
        slug, title, body, (domain or None), _agents(applies_to), status,
        source_candidate_id, source_url, procedure_path,
        provenance, (source_ref or source_url),
    )
    invalidate_cache()
    return sid


async def set_status(skill_id: UUID, status: str, *, reason: str | None = None) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    retired_at = datetime.now(UTC) if status == "retired" else None
    row = await db.fetchrow(
        """
        update skills
        set status = $2, retired_at = $3,
            retire_reason = case when $2 = 'retired' then $4 else null end,
            updated_at = now()
        where id = $1 returning id
        """,
        skill_id, status, retired_at, (reason or "")[:500] or None,
    )
    invalidate_cache()
    return row is not None


async def retire_skill(skill_id: UUID, reason: str | None = None) -> bool:
    return await set_status(skill_id, "retired", reason=reason)


async def activate_skill(skill_id: UUID) -> bool:
    return await set_status(skill_id, "active")


async def review_skill(skill_id: UUID, decision: str, *, note: str | None = None,
                       reviewer: str = "trilok") -> dict[str, Any]:
    """Record Trilok's approval of a skill, or its revocation. Provenance is
    untouched. Only an approved agent_reflection skill becomes injectable;
    approving an internet-derived or unknown one records the review and
    changes nothing else."""
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"decision must be one of {REVIEW_DECISIONS}")
    if await get_skill(skill_id) is None:
        raise LookupError(f"no skill {skill_id}")
    await db.execute(
        "insert into skill_reviews (skill_id, decision, reviewer, note) values ($1, $2, $3, $4)",
        skill_id, decision, reviewer, (note or "")[:500] or None,
    )
    invalidate_cache()
    skill = await get_skill(skill_id) or {}
    return {**skill, "injectable": injectable(skill.get("provenance"), skill.get("review"))}


async def remove_skill(skill_id: UUID) -> bool:
    row = await db.fetchrow("delete from skills where id = $1 returning id", skill_id)
    invalidate_cache()
    return row is not None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


_COLS = (
    "id, slug, title, body, domain, applies_to, status, source_candidate_id, "
    "source_url, procedure_path, uses, good, bad, last_used_at, retired_at, "
    "retire_reason, created_at, updated_at, provenance, source_ref, "
    "(select r.decision from skill_reviews r where r.skill_id = skills.id "
    "order by r.created_at desc limit 1) as review"
)


async def list_skills(status: str | None = None, *, agent: str | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    rows = await db.fetch(
        f"""
        select {_COLS} from skills
        where ($1::text is null or status = $1)
          and ($2::text is null or $2 = any(applies_to) or 'all' = any(applies_to))
        order by (status = 'retired'), created_at desc
        limit $3
        """,
        status, agent, limit,
    )
    return [dict(r) for r in rows]


async def get_skill(skill_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(f"select {_COLS} from skills where id = $1", skill_id)
    return dict(row) if row else None


async def skills_for(agent: str, *, force: bool = False) -> list[dict[str, Any]]:
    """The skills that may be injected for `agent` (see the module doc),
    cached briefly."""
    agent = (agent or "").strip().lower()
    now = time.monotonic()
    hit = _cache.get(agent)
    if not force and hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    rows = await db.fetch(
        """
        select s.id, s.title, s.body, s.provenance from skills s
        where s.status in ('trial', 'active')
          and ($1 = any(s.applies_to) or 'all' = any(s.applies_to))
          and (s.provenance = 'user_authored'
               or (s.provenance = 'agent_reflection'
                   and (select r.decision from skill_reviews r where r.skill_id = s.id
                        order by r.created_at desc limit 1) = 'approved'))
        order by s.created_at
        """,
        agent,
    )
    out = [dict(r) for r in rows]
    _cache[agent] = (now, out)
    return out


def format_block(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["Playbook: rules Trilok wrote, and practices Atlas drew from its own runs that he approved."]
    for r in rows:
        body = " ".join((r.get("body") or "").split())
        who = "Trilok" if r.get("provenance") == "user_authored" else "Atlas, approved by Trilok"
        lines.append(f"- {r['title']} ({who}): {body}")
    return "\n".join(lines)


async def skills_block(agent: str) -> tuple[str, list[UUID]]:
    """The injectable block for `agent` and the ids it carries."""
    rows = await skills_for(agent)
    return format_block(rows), [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Uses and feedback
# ---------------------------------------------------------------------------


async def record_uses(episode_id: UUID, skill_ids: list[UUID]) -> int:
    """Remember that `episode_id` ran with these skills in its prompt."""
    if not skill_ids:
        return 0
    ids = list(dict.fromkeys(skill_ids))
    await db.execute(
        """
        insert into skill_uses (skill_id, episode_id)
        select unnest($1::uuid[]), $2 on conflict do nothing
        """,
        ids, episode_id,
    )
    await db.execute(
        "update skills set uses = uses + 1, last_used_at = now() where id = any($1::uuid[])",
        ids,
    )
    return len(ids)


async def _recount(skill_ids: list[UUID]) -> None:
    if not skill_ids:
        return
    await db.execute(
        """
        update skills s set
          good = (select count(*) from skill_uses u join episode_feedback f using (episode_id)
                  where u.skill_id = s.id and f.verdict = 'good'),
          bad  = (select count(*) from skill_uses u join episode_feedback f using (episode_id)
                  where u.skill_id = s.id and f.verdict = 'bad'),
          updated_at = now()
        where s.id = any($1::uuid[])
        """,
        skill_ids,
    )


async def rate_episode(episode_id: UUID, verdict: str, *, note: str | None = None,
                       source: str = "owner") -> list[UUID]:
    """Record good / bad on an episode and push it onto the skills that ran
    in it. Returns the ids of the skills affected. Raises ValueError on an
    unknown episode or verdict."""
    verdict = (verdict or "").strip().lower()
    if verdict not in ("good", "bad"):
        raise ValueError("verdict must be good or bad")
    if not await db.fetchval("select 1 from episodes where id = $1", episode_id):
        raise ValueError(f"no episode {episode_id}")
    await db.execute(
        """
        insert into episode_feedback (episode_id, verdict, note, source)
        values ($1, $2, $3, $4)
        on conflict (episode_id) do update
          set verdict = excluded.verdict, note = excluded.note,
              source = excluded.source, created_at = now()
        """,
        episode_id, verdict, (note or "").strip()[:1000] or None, source,
    )
    ids = [r["skill_id"] for r in await db.fetch(
        "select skill_id from skill_uses where episode_id = $1", episode_id)]
    await _recount(ids)
    return ids


# ---------------------------------------------------------------------------
# Evals as the measurement
# ---------------------------------------------------------------------------


async def record_eval_results(suite: str, results: list[Any]) -> int:
    """Persist one eval run so pass rates can be compared over time."""
    n = 0
    for r in results:
        await db.execute(
            'insert into eval_runs (suite, "case", passed, details) values ($1, $2, $3, $4)',
            suite, getattr(r, "case", "?"), bool(getattr(r, "passed", False)),
            [str(d) for d in getattr(r, "details", [])][:20],
        )
        n += 1
    return n


async def eval_delta(agent: str, since: datetime) -> dict[str, Any]:
    """Pass rate for the agent's eval suite before and after `since`."""
    row = await db.fetchrow(
        """
        select
          count(*) filter (where created_at <  $2) as before_n,
          count(*) filter (where created_at <  $2 and passed) as before_pass,
          count(*) filter (where created_at >= $2) as after_n,
          count(*) filter (where created_at >= $2 and passed) as after_pass
        from eval_runs where suite = $1
        """,
        agent, since,
    )
    def rate(p: int, n: int) -> float | None:
        return (p / n) if n else None
    return {
        "before": rate(row["before_pass"], row["before_n"]), "before_n": row["before_n"],
        "after": rate(row["after_pass"], row["after_n"]), "after_n": row["after_n"],
    }


# ---------------------------------------------------------------------------
# The Sunday report
# ---------------------------------------------------------------------------


async def skill_report() -> list[dict[str, Any]]:
    """Every trial and active skill with its numbers and a suggestion:
    'keep', 'retire', or 'wait' (not enough runs yet). Trilok decides."""
    rows = await list_skills(status=None, limit=200)
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["status"] == "retired":
            continue
        uses, good, bad = r["uses"], r["good"], r["bad"]
        if r["status"] == "active":
            suggestion = "retire" if bad > good and bad >= 3 else "keep"
        elif uses < settings.skill_trial_runs:
            suggestion = "wait"
        elif bad > good:
            suggestion = "retire"
        else:
            suggestion = "keep"
        agents = [a for a in r["applies_to"] if a != "all"]
        delta = None
        if len(agents) == 1:
            delta = await eval_delta(agents[0], r["created_at"])
            if delta["before"] is None and delta["after"] is None:
                delta = None
        out.append({
            "id": r["id"], "slug": r["slug"], "title": r["title"], "status": r["status"],
            "applies_to": r["applies_to"], "uses": uses, "good": good, "bad": bad,
            "suggestion": suggestion, "eval": delta, "created_at": r["created_at"],
        })
    return out


def format_report(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["Playbook in force:"]
    for r in rows:
        who = ", ".join(r["applies_to"])
        line = (f"- {r['title']} [{r['status']}, {who}]: used {r['uses']}x, "
                f"{r['good']} good, {r['bad']} bad")
        ev = r.get("eval")
        if ev and ev.get("before") is not None and ev.get("after") is not None:
            line += f", evals {ev['before']:.0%} -> {ev['after']:.0%}"
        if r["suggestion"] == "retire":
            line += ". Suggest retiring."
        elif r["suggestion"] == "wait":
            line += f". Still in trial ({settings.skill_trial_runs} runs to judge)."
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "STATUSES", "PROVENANCES", "REVIEW_DECISIONS", "provenance_for_source_type", "injectable",
    "review_skill", "add_skill", "set_status", "retire_skill", "activate_skill", "remove_skill",
    "list_skills", "get_skill", "skills_for", "format_block", "skills_block",
    "record_uses", "rate_episode", "record_eval_results", "eval_delta",
    "skill_report", "format_report", "invalidate_cache",
]
