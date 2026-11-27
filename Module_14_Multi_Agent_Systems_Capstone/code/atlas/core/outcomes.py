"""Atlas -- outcome learning: every result that comes back lands on the run
that caused it.

Four kinds of result come back to Atlas. A tap on a trace (good or bad).
The morning review of a night task (accepted or rework). A company's reply
to an application the career agent drafted (a screen, an interview, an
offer, or a no). A correction in chat ("that draft was wrong, the lender is
Daniel"). Each one is tied to the episode that produced the work, lands on
every playbook skill that was in force in that run (so the Sunday review
can tell which learnings help), and, when it is bad, proposes an eval case:
the task, what came out, and what he said, kept until it is written into
evals/data by scripts/eval_candidates.py or dismissed. The Atlas Standard's
learning row counts the loop.

Nothing here raises into the caller that produced the outcome: a review or
a mail scan must never fail because the learning could not be recorded.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from core import db, events
from core.skills import rate_episode

log = logging.getLogger(__name__)

KINDS = ("tap", "night_review", "application", "correction")
# Application moves that say something about the draft that got them.
_APPLICATION_VERDICTS = {"screening": "good", "interview": "good", "offer": "good", "rejected": "bad"}


async def record(episode_id: UUID, verdict: str, *, kind: str, note: str | None = None,
                 source: str = "owner") -> dict[str, Any]:
    """Tie one result to its run. Returns {recorded, skills, candidate_id}.
    Raises ValueError on an unknown episode, verdict or kind."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    skills = await rate_episode(episode_id, verdict, note=note, source=source)
    verdict = verdict.strip().lower()
    await db.execute("update episode_feedback set kind = $2 where episode_id = $1", episode_id, kind)
    candidate_id = None
    if verdict == "bad":
        candidate_id = await propose_eval(episode_id, note=note, kind=kind, source=source)
    row = await db.fetchrow("select agent_name, summary from episodes where id = $1", episode_id)
    await events.emit("outcome", agent=row["agent_name"] if row else None, episode_id=episode_id,
                      title=(row["summary"] or "")[:160] if row else "",
                      detail=(note or "")[:400], data={"verdict": verdict, "kind": kind, "source": source})
    return {"recorded": True, "verdict": verdict, "kind": kind, "skills": skills, "candidate_id": candidate_id}


async def from_night_review(task: dict[str, Any], verdict: str, note: str | None) -> None:
    """The morning verdict on a night task, on the run that did it. Never raises."""
    ep = task.get("episode_id")
    if not ep:
        return
    try:
        await record(UUID(str(ep)), "good" if verdict == "accepted" else "bad", kind="night_review",
                     note=note or f"{verdict} in the morning review", source="owner/night")
    except Exception:
        log.exception("outcomes: night review of %s did not land", task.get("id"))


async def from_application(app: dict[str, Any], before: str | None, subject: str = "") -> None:
    """A company's reply, on the draft that got it. Never raises."""
    status = app.get("status")
    verdict = _APPLICATION_VERDICTS.get(str(status))
    if verdict is None or status == before:
        return
    try:
        ep = await db.fetchval(
            "select draft_episode_id from job_queue where application_id = $1 and draft_episode_id is not null "
            "order by updated_at desc limit 1",
            app["id"],
        )
        if ep is None:
            return
        note = f"{app.get('company', '')}: {before or 'applied'} -> {status}" + (f". {subject[:200]}" if subject else "")
        await record(ep, verdict, kind="application", note=note, source="job_mail")
    except Exception:
        log.exception("outcomes: application %s did not land", app.get("id"))


async def propose_eval(episode_id: UUID, *, note: str | None, kind: str, source: str) -> UUID | None:
    """A bad outcome becomes a candidate eval case from the run's own record."""
    ep = await db.fetchrow("select agent_name, summary, result, status from episodes where id = $1", episode_id)
    if ep is None:
        return None
    task = await db.fetchval(
        "select inputs->>'task' from actions where episode_id = $1 and action_type = 'dispatch' "
        "order by created_at limit 1",
        episode_id,
    )
    existing = await db.fetchval(
        "select id from eval_candidates where episode_id = $1 and status = 'proposed'", episode_id)
    if existing:
        await db.execute("update eval_candidates set note = coalesce($2, note), kind = $3 where id = $1",
                         existing, (note or "").strip()[:1000] or None, kind)
        return existing
    return await db.fetchval(
        """
        insert into eval_candidates (episode_id, agent, task, result, note, verdict, kind, source)
        values ($1, $2, $3, $4, $5, 'bad', $6, $7) returning id
        """,
        episode_id, ep["agent_name"], (task or ep["summary"] or "")[:4000], (ep["result"] or "")[:4000],
        (note or "").strip()[:1000] or None, kind, source,
    )


async def candidates(status: str | None = "proposed", limit: int = 50) -> list[dict[str, Any]]:
    where = "where status = $2" if status else ""
    args: list[Any] = [max(1, min(int(limit), 200))]
    if status:
        args.append(status)
    rows = await db.fetch(
        f"select id, episode_id, agent, task, result, note, verdict, kind, source, status, suite, created_at, decided_at "
        f"from eval_candidates {where} order by created_at desc limit $1",
        *args,
    )
    return [dict(r) for r in rows]


async def set_candidate(candidate_id: UUID, status: str, *, suite: str | None = None) -> dict[str, Any] | None:
    if status not in ("proposed", "written", "dismissed"):
        raise ValueError("status must be proposed, written or dismissed")
    row = await db.fetchrow(
        "update eval_candidates set status = $2, suite = coalesce($3, suite), "
        "decided_at = case when $2 = 'proposed' then null else now() end where id = $1 returning id",
        candidate_id, status, suite,
    )
    if row is None:
        return None
    found = await candidates(status=None, limit=200)
    return next((c for c in found if c["id"] == candidate_id), None)


async def summary(days: int = 7) -> dict[str, Any]:
    """This week against last: outcomes by kind and verdict, and the eval loop."""
    now = datetime.now(UTC)
    this_start, last_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    rows = await db.fetch(
        """
        select kind, verdict, count(*) filter (where created_at >= $1) as this_week,
               count(*) filter (where created_at >= $2 and created_at < $1) as last_week
        from episode_feedback where created_at >= $2 group by kind, verdict order by kind, verdict
        """,
        this_start, last_start,
    )
    by_kind: dict[str, dict[str, int]] = {}
    totals = {"this": {"good": 0, "bad": 0}, "last": {"good": 0, "bad": 0}}
    for r in rows:
        slot = by_kind.setdefault(r["kind"], {"good": 0, "bad": 0})
        slot[r["verdict"]] += int(r["this_week"])
        totals["this"][r["verdict"]] += int(r["this_week"])
        totals["last"][r["verdict"]] += int(r["last_week"])
    cand = await db.fetchrow(
        "select count(*) filter (where status = 'proposed') as proposed, count(*) filter (where status = 'written') as written, "
        "count(*) filter (where status = 'dismissed') as dismissed from eval_candidates"
    )

    def ratio(t: dict[str, int]) -> float | None:
        n = t["good"] + t["bad"]
        return round(t["good"] / n, 3) if n else None

    return {"days": days, "by_kind": by_kind, "this_week": {**totals["this"], "good_ratio": ratio(totals["this"])},
            "last_week": {**totals["last"], "good_ratio": ratio(totals["last"])},
            "candidates": {k: int(cand[k] or 0) for k in ("proposed", "written", "dismissed")}}


def to_jsonl(rows: list[dict[str, Any]]) -> str:
    """Candidates as eval-case lines: the task, what should not happen again, and the record."""
    out = []
    for c in rows:
        out.append(json.dumps({
            "id": str(c["id"]), "agent": c["agent"], "task": c["task"], "bad_result": c["result"],
            "expected": c["note"], "kind": c["kind"], "episode_id": str(c["episode_id"]) if c["episode_id"] else None,
            "recorded_at": c["created_at"].isoformat() if c.get("created_at") else None,
        }, ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else "")


__all__ = ["KINDS", "record", "from_night_review", "from_application", "propose_eval", "candidates",
           "set_candidate", "summary", "to_jsonl"]
