"""Atlas's checks on an engine result, applied after the engine and before the draft is kept.

The summary guard: the engine sometimes rewrites a full summary into one line, or
drops the title the candidate leads with ("AI engineer who ..."). The judge scores
those drafts lower and notes the weak summary, but keeps the rest of the edits, so
the collapsed summary would ship. The guard keeps the original summary when the
rewrite cuts it by more than 40 percent of its words or drops the role title, and
re-renders the engine's other edits unchanged. The engine files are not touched:
this works on the result dict, with the engine's own Doc.render.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.resume_engine import apex_engine as E

log = logging.getLogger(__name__)

MAX_CUT = 0.40
# Words that end the title a summary leads with ("AI engineer who takes ...").
_TITLE_END = {"who", "with", "that", "and", "focused", "specializing", "building", "bringing", "since",
              "for", "at", "in", "from", "of", "whose", "looking", "experienced"}
# A title ends in a role noun; anything else is not treated as a title.
_ROLE_NOUN = re.compile(
    r"(engineer|developer|programmer|analyst|scientist|manager|designer|consultant|architect|specialist|"
    r"lead|director|administrator|coordinator|advisor|officer|technician|accountant|therapist|nurse|"
    r"researcher|strategist|marketer|writer|editor|recruiter|teacher|instructor|executive|founder|owner|"
    r"operator|associate|assistant|representative|planner|producer)s?$", re.I)


def summary_title(summary: str) -> str | None:
    """The role title a summary leads with, or None when it does not lead with one."""
    first = re.split(r"(?<=[.!?;])\s|,", (summary or "").strip(), maxsplit=1)[0]
    words: list[str] = []
    for w in first.split():
        if w.lower().strip(".,;:") in _TITLE_END:
            break
        words.append(w.strip(".,;:"))
        if len(words) > 6:
            return None
    title = " ".join(words)
    return title if words and _ROLE_NOUN.search(title) else None


def summary_guard(engine_text: str, result: dict[str, Any]) -> str | None:
    """Keep the original summary when the rewrite collapses it. Mutates `result`
    (final, changes, summary_guard) and returns the reason, or None when it passes."""
    change = next((c for c in result.get("changes") or [] if c.get("id") == "S"), None)
    if change is None:
        return None
    doc = E.Doc(engine_text)
    original = doc.by_id.get("S")
    if not original or original.get("new") or not original.get("text"):
        return None  # a created summary has nothing to keep
    before, after = original["text"], " ".join(change.get("after") or [])
    reasons = []
    n_before, n_after = len(before.split()), len(after.split())
    if n_after < (1 - MAX_CUT) * n_before:
        reasons.append(f"the rewrite cut it from {n_before} to {n_after} words")
    title = summary_title(before)
    if title and title.lower() not in after.lower():
        reasons.append(f'the rewrite dropped your role title "{title}"')
    if not reasons:
        return None
    edits = {c["id"]: c["after"] for c in result["changes"] if c.get("id") != "S" and c.get("after") is not None}
    result["final"] = doc.render(edits, result.get("order") or (), result.get("section_order"))
    result["changes"] = [c for c in result["changes"] if c.get("id") != "S"]
    reason = "Kept your original summary: " + "; ".join(reasons) + "."
    result["summary_guard"] = reason
    log.info("resume engine summary guard: %s", reason)
    return reason


__all__ = ["MAX_CUT", "summary_guard", "summary_title"]
