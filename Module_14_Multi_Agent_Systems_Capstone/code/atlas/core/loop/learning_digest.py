"""Atlas -- nightly learning digest (Gate 1).

After the discovery scan proposes candidates, this sends Trilok the
nightly digest: every proposed learning in one notification, with the
candidate ids in its data so the PWA can offer Pursue / Skip per item.
Pursue moves a candidate to 'pursuing' (Gate 2, the CTO draft, is next);
Skip rejects it. The decisions themselves land through the web API.

run_learning_digest never raises, so a bad digest cannot stop the scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

from core import discovery, skills
from core.surfaces.notify import notify

log = logging.getLogger(__name__)

MAX_CARDS = 20  # cap a single night's digest so it never floods the inbox


def format_candidate(c: dict[str, Any]) -> str:
    """One candidate as a few plain lines."""
    title = (c.get("title") or "").strip() or "(untitled)"
    creator = (c.get("creator") or "").strip()
    head = f"- {title}" + (f" ({creator})" if creator else "")
    scores = []
    for key in ("relevance", "novelty", "credibility"):
        val = c.get(key)
        if isinstance(val, (int, float)):
            scores.append(f"{key} {float(val):.1f}")
    if scores:
        head += "  [" + ", ".join(scores) + "]"
    lines = [head]
    learning = (c.get("learning") or "").strip()
    if learning:
        lines.append(f"  {learning[:300]}")
    if c.get("source_url"):
        lines.append(f"  {c['source_url']}")
    return "\n".join(lines)


async def send_learning_candidates(candidates: list[dict[str, Any]]) -> list[Any]:
    """Send the digest. Returns the ids that were included, or [] on failure."""
    if not candidates:
        return []
    body = "\n\n".join(format_candidate(c) for c in candidates)
    res = await notify(
        kind="learning_digest",
        title=f"{len(candidates)} learning{'s' if len(candidates) != 1 else ''} to review",
        body=body,
        data={"candidate_ids": [str(c["id"]) for c in candidates]},
    )
    return [c["id"] for c in candidates] if res is not None else []


async def run_learning_digest() -> None:
    """Scheduler entry: send proposed candidates as one digest."""
    try:
        candidates = await discovery.list_undigested(limit=MAX_CARDS)
    except Exception:
        log.exception("learning digest: could not load candidates")
        return
    if not candidates:
        log.info("learning digest: nothing new to send tonight")
        return
    try:
        sent_ids = await send_learning_candidates(candidates)
    except Exception:
        log.exception("learning digest: send failed")
        return
    if sent_ids:
        try:
            await discovery.mark_digested(sent_ids)
        except Exception:
            log.exception("learning digest: could not mark candidates digested")
    log.info("learning digest: sent %d of %d candidate(s)", len(sent_ids), len(candidates))


async def weekly_learnings_review() -> None:
    """Sunday: summarize the week's learnings (the funnel, the adopted wins,
    and how each skill in the playbook is doing) and send it to Trilok.
    Never raises."""
    try:
        summ = await discovery.weekly_summary(7)
    except Exception:
        log.exception("weekly learnings review: could not build summary")
        return

    bs = summ["by_status"]
    seen = sum(bs.values())
    adopted = summ["adopted"]
    pending = summ["pending_total"]

    lines = [f"Atlas learning review, last {summ['days']} days", ""]
    if seen == 0 and not adopted and not pending:
        lines.append("Nothing new this week.")
    else:
        lines.append(
            f"{seen} new candidate(s) this week: "
            f"{bs.get('proposed', 0)} awaiting review, "
            f"{bs.get('pursuing', 0)} pursuing, "
            f"{bs.get('adopted', 0)} adopted, "
            f"{bs.get('rejected', 0)} skipped."
        )
        if adopted:
            lines.append("")
            lines.append("Adopted into Atlas:")
            for a in adopted:
                lines.append(f"- {a.get('learning')}")
        if pending:
            lines.append("")
            lines.append(f"{pending} learning(s) waiting for your review.")

    try:
        report = skills.format_report(await skills.skill_report())
    except Exception:
        log.exception("weekly learnings review: could not build the skill report")
        report = ""
    if report:
        lines.append("")
        lines.append(report)
        lines.append("Keep or retire from the Playbook panel in Atlas.")

    text = "\n".join(lines)
    try:
        res = await notify(kind="learning_review", title="Weekly learning review", body=text)
    except Exception:
        log.exception("weekly learnings review: send failed")
        return
    if res is None:
        log.warning("weekly learnings review: notification not stored")
        return
    log.info("weekly learnings review sent (%d new, %d adopted, %d pending)",
             seen, bs.get("adopted", 0), pending)


__all__ = [
    "format_candidate",
    "send_learning_candidates",
    "run_learning_digest",
    "weekly_learnings_review",
]
