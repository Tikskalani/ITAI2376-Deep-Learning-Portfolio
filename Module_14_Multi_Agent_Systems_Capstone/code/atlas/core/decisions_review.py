"""Atlas -- the Sunday decision-review loop.

A decision in the journal is only useful if it gets revisited. The CEO's
weekly review *mentions* open decisions in narrative form; this module
turns that mention into a forced loop. For every open decision older than
the threshold, the owner is asked Right / Wrong / Mixed. The answer:
  * closes the decision via resolve_decision
  * records a lesson, with "correction:" prefix and metadata when the
    assessment is wrong, so the lesson search surfaces past mistakes the
    next time an agent works in the same domain.

Pure logic, no Telegram. The Telegram surface composes this with inline
buttons; tests can exercise the loop without touching the bot.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from core import db
from core.decisions import _COLS, resolve_decision
from core.lessons import record_lesson

log = logging.getLogger(__name__)


# Assessments accepted by the review loop. resolve_decision also accepts
# "unclear", but the review UI offers only the three actionable verdicts.
REVIEW_ASSESSMENTS = ("right", "wrong", "mixed")

# Default age threshold: a decision is worth reviewing only after it has
# had time to play out. Anything younger than this is too fresh to score.
DEFAULT_MIN_AGE_HOURS = 24


async def list_reviewable(
    min_age_hours: int = DEFAULT_MIN_AGE_HOURS,
) -> list[dict[str, Any]]:
    """Open decisions that are due for review, oldest first.

    A decision counts as reviewable when either:
      * its review_at date is set and has arrived, or
      * its review_at is unset and decided_at is older than the age
        cutoff (default 24 hours).

    Decisions logged with an explicit future review_at stay silent until
    that date, so a "check the first invoice on July 1" decision does
    not nag tomorrow. Decisions logged without a review_at fall back to
    the time-based cooldown so the loop still surfaces them in normal
    use.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=min_age_hours)
    rows = await db.fetch(
        f"""
        select {_COLS} from decisions
        where status = 'open' and (
            (review_at is not null and review_at <= current_date)
            or (review_at is null and decided_at <= $1)
        )
        order by decided_at asc
        """,
        cutoff,
    )
    return [dict(r) for r in rows]


def _lesson_for(decision: dict[str, Any], assessment: str) -> str:
    """Build the lesson content from a resolved decision and its verdict.

    Voice rules: natural prose, no em-dash, no AI tells. Lessons are stored
    in the lessons table and surface in future agent runs as context, so
    they need to read like something a thoughtful human wrote.
    """
    summary = (decision.get("summary") or "(no summary)").strip()
    rationale = (decision.get("rationale") or "no rationale recorded").strip()
    today = datetime.now(UTC).date().isoformat()

    if assessment == "right":
        return (
            f"Validated past decision: {summary}. "
            f"The reasoning held: {rationale}."
        )
    if assessment == "wrong":
        return (
            f"correction: {summary}. The rationale did not hold up: "
            f"{rationale}. Reviewed wrong on {today}."
        )
    if assessment == "mixed":
        return (
            f"Partial: {summary}. Outcome was mixed against the "
            f"rationale: {rationale}. Reviewed on {today}."
        )
    # Defensive fallback; record_review validates before getting here.
    return f"Reviewed: {summary}. Verdict: {assessment}."


async def record_review(
    decision_id: UUID,
    assessment: str,
    outcome: str | None = None,
) -> bool:
    """Close a decision and write a corresponding lesson.

    Returns True if a lesson was recorded, False if the decision was
    already resolved or expired (idempotent). The lesson carries the
    decision_id and assessment in metadata so the source can be
    reconstructed later.

    Raises ValueError if `assessment` is not one of right / wrong / mixed.
    """
    if assessment not in REVIEW_ASSESSMENTS:
        raise ValueError(
            f"invalid review assessment {assessment!r}; "
            f"expected one of {REVIEW_ASSESSMENTS}"
        )

    # Resolve first so the no-op case (already resolved) short-circuits
    # without writing a stale lesson.
    outcome_text = outcome or f"Reviewed on Sunday as {assessment}."
    changed = await resolve_decision(decision_id, outcome_text, assessment)
    if not changed:
        log.info(
            "decision %s already resolved; no review lesson recorded",
            decision_id,
        )
        return False

    # Fetch the resolved decision so the lesson can quote it.
    row = await db.fetchrow(
        f"select {_COLS} from decisions where id = $1", decision_id
    )
    decision = dict(row) if row else {}

    content = _lesson_for(decision, assessment)
    domain = decision.get("domain") or "decision-review"
    metadata = {
        "source": "weekly_review",
        "decision_id": str(decision_id),
        "assessment": assessment,
        "correction": assessment == "wrong",
    }
    await record_lesson(
        content=content,
        domain=domain,
        source_episode=decision.get("episode_id"),
        metadata=metadata,
    )
    return True


async def send_review_round() -> int:
    """Notify the owner of every decision due for review, as one card.

    The PWA renders Right / Wrong / Mixed per decision from the ids in
    data; record_review lands the verdict. Returns how many decisions
    were included; 0 when nothing is due or the notification failed.
    Never raises.
    """
    from core.surfaces.notify import notify

    try:
        rows = await list_reviewable()
    except Exception:
        log.exception("review round: could not list reviewable decisions")
        return 0
    if not rows:
        return 0
    rows = rows[:20]
    body = "\n\n".join(review_prompt_text(d) for d in rows)
    try:
        res = await notify(
            kind="decision_review",
            title=f"{len(rows)} decision{'s' if len(rows) != 1 else ''} to review",
            body=body,
            data={"decisions": [{"id": str(d["id"]), "summary": d.get("summary")} for d in rows]},
        )
    except Exception:
        log.exception("review round: notify failed")
        return 0
    return len(rows) if res is not None else 0


def review_prompt_text(decision: dict[str, Any]) -> str:
    """Render the prompt for a single decision in the review round.

    Kept here so the wording stays alongside the lesson templates and the
    surface stays thin.
    """
    summary = (decision.get("summary") or "(no summary)").strip()
    rationale = (decision.get("rationale") or "").strip()
    decided_at = decision.get("decided_at")
    when = (
        decided_at.astimezone().strftime("%b %d") if decided_at else "?"
    )
    expected = (decision.get("expected_outcome") or "").strip()

    lines = [
        f"Decision review ({when}):",
        summary,
    ]
    if rationale:
        lines.append(f"Why: {rationale}")
    if expected:
        lines.append(f"Expected: {expected}")
    lines.append("")
    lines.append("How did this call hold up?")
    return "\n".join(lines)


__all__ = [
    "REVIEW_ASSESSMENTS",
    "DEFAULT_MIN_AGE_HOURS",
    "list_reviewable",
    "record_review",
    "review_prompt_text",
]
