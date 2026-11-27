"""The one-page master: a selection of cuts from the full master, never a second resume.

The full master in data/resume/ stays the one source of truth (precheck checks
every draft against it). The one-page master is stored only as the list of
bullets Trilok chose to cut, in data/resume/one_page.json, with the SHA-256 of
the full master they were cut from. Its text is rebuilt from the full master
each time; if the master changed since the cuts were approved, the selection is
stale and refused until he approves again.

propose() runs the engine's own cut stage once (apex_engine._fit_budget on the
scan model, a few cents at most) and lists every bullet with the ones it would
cut marked. apply() takes the ids Trilok picked, checks the result fits one
page and every role keeps a bullet, and writes the selection.

evidence() is how the cut lines still count: each one becomes a fact for its own
role (target['facts'], split into sentences under the engine's 600-character
fact limit), so the engine may draw on everything in the full master while it
rewrites the one-page text.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.jobs import resume_pdf
from core.resume_engine import apex_domains, bridge
from core.resume_engine import apex_engine as E

SELECTION_NAME = "one_page.json"
PROPOSAL_NAME = "one_page.proposal.json"
_FACT_CHARS = 600  # apex_engine.Doc.attach_facts keeps the first 600 characters of a fact


class OnePageError(ValueError):
    """The selection cannot be used. The message says what to do."""


def master_hash(full_format: str) -> str:
    return hashlib.sha256(full_format.encode("utf-8")).hexdigest()


def bullets(full_format: str) -> list[dict[str, Any]]:
    """Every bullet the engine can cut: id, role, text, words."""
    doc = E.Doc(full_format)
    return [{"id": b["id"], "role": b["group"].split(" | ")[0].removeprefix("### "),
             "text": b["text"], "words": len(b["text"].split())}
            for b in doc.blocks if b["kind"] == "bullet"]


def _words(text: str) -> int:
    return len(text[text.find("# "):].split()) if "# " in text else len(text.split())


def _render_without(full_format: str, cut: list[str]) -> str:
    doc = E.Doc(full_format)
    unknown = [i for i in cut if i not in doc.by_id or doc.by_id[i]["kind"] != "bullet"]
    if unknown:
        raise OnePageError(f"not bullet ids of the master: {', '.join(unknown)}")
    groups: dict[str, int] = {}
    for b in doc.blocks:
        if b["kind"] == "bullet" and b["id"] not in cut:
            groups[b["group"]] = groups.get(b["group"], 0) + 1
    emptied = {doc.by_id[i]["group"] for i in cut} - set(groups)
    if emptied:
        names = sorted(g.split(" | ")[0].removeprefix("### ") for g in emptied)
        raise OnePageError("every role keeps at least one bullet; these would have none: "
                           + "; ".join(names))
    return doc.render({i: [] for i in cut})


async def propose(
    full_format: str,
    *,
    title: str = "AI Engineer",
    cap_usd: Decimal = Decimal("0.05"),
    config: bridge.EngineConfig | None = None,
    complete: bridge.CompleteFn | None = None,
) -> dict[str, Any]:
    """The cut stage's suggestion, for Trilok to accept, change or reject."""
    config = config or bridge.load_config()
    doc = E.Doc(full_format)
    target = {"title": title, "domain": apex_domains.detect(title), "seniority": "mid", "supported": []}
    lines: list[str] = []

    def cut(engine_llm: bridge.EngineLLM) -> dict[str, Any]:
        found: dict[str, Any] = E._fit_budget(engine_llm, doc, target, {}, [], lines.append)
        return found

    cuts, meter = await bridge.call_engine(cut, cap_usd=cap_usd, config=config, complete=complete)
    cuts = cuts or {}
    listing = bullets(full_format)
    for b in listing:
        c = cuts.get(b["id"])
        b["proposed_cut"] = c is not None
        b["why"] = c["why"] if c else ""
    proposed = [b["id"] for b in listing if b["proposed_cut"]]
    after = _render_without(full_format, proposed) if proposed else full_format
    return {
        "master_sha256": master_hash(full_format),
        "words": _words(full_format),
        "page_words": config.page_words,
        "proposed": proposed,
        "words_after": _words(after),
        "pages_after": resume_pdf.render(after).pages,
        "bullets": listing,
        "spent_usd": str(meter.spent_usd),
        "stopped": meter.stopped,
    }


def apply(full_format: str, cut: list[str], directory: Path) -> str:
    """Write the selection Trilok approved. Returns the one-page text.
    Refuses a selection that leaves a role empty or still runs past one page."""
    text = _render_without(full_format, cut)
    rendered = resume_pdf.render(text)
    if not rendered.fits:
        raise OnePageError(f"still {_words(text)} words and {rendered.pages} pages at "
                           f"{resume_pdf.SIZES[-1]}pt; cut a few more bullets")
    doc = E.Doc(full_format)
    selection = {
        "master_sha256": master_hash(full_format),
        "cut": [{"id": i, "text": doc.by_id[i]["text"]} for i in cut],
        "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (directory / SELECTION_NAME).write_text(json.dumps(selection, indent=2), encoding="utf-8")
    return text


def load(full_format: str, directory: Path) -> str | None:
    """The one-page text, rebuilt from the full master, or None when no
    selection was approved. Raises OnePageError when it is stale."""
    path = directory / SELECTION_NAME
    if not path.is_file():
        return None
    selection = json.loads(path.read_text(encoding="utf-8"))
    if selection.get("master_sha256") != master_hash(full_format):
        raise OnePageError("the master resume changed since the one-page cuts were approved; "
                           "run python -m scripts.resume_one_page propose again")
    doc = E.Doc(full_format)
    cut = [c["id"] for c in selection.get("cut") or []]
    if any(doc.by_id.get(c["id"], {}).get("text") != c["text"] for c in selection["cut"]):
        raise OnePageError("the approved cuts no longer match the master's bullets; approve them again")
    return _render_without(full_format, cut)


def _sentences(text: str) -> list[str]:
    """Split a long line into pieces under the fact limit, at sentence ends."""
    if len(text) <= _FACT_CHARS:
        return [text]
    out, cur = [], ""
    for part in re.split(r"(?<=[.;])\s+", text):
        if cur and len(cur) + 1 + len(part) > _FACT_CHARS:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    if cur:
        out.append(cur)
    return [p[:_FACT_CHARS] for p in out]


def evidence(full_format: str, one_page_format: str) -> list[dict[str, str]]:
    """The full master's bullets that the one-page text left out, as facts for
    their own roles (topic = the role line, which the engine matches)."""
    kept = {b["text"] for b in E.Doc(one_page_format).blocks}
    facts = []
    for b in E.Doc(full_format).blocks:
        if b["kind"] == "bullet" and b["text"] not in kept:
            topic = b["group"].split(" | ")[0].removeprefix("### ")
            facts += [{"topic": topic, "text": piece} for piece in _sentences(b["text"])]
    return facts


__all__ = ["OnePageError", "PROPOSAL_NAME", "SELECTION_NAME", "apply", "bullets", "evidence",
           "load", "master_hash", "propose"]
