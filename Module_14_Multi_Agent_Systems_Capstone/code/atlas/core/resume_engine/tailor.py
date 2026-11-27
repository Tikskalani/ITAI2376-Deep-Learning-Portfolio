"""One application through the evidence engine: draft, questions, answers, revision.

optimize_application() builds the engine's target from the posting, reads the
posting's requirements (the keywords stage, inside the run's cap), runs the
engine on the one-page master with everything the full master has as evidence,
checks the draft with precheck's resume half, and saves it on the application
with the engine's record. The engine's questions go into candidate_answers
(context kind "resume") with one notification for the run.

revise_application() reads the answers Trilok typed to those questions in the
Jobs tab and rewrites only those lines. It takes no answer text from the agent:
an answer the model wrote could put words in his mouth, so the only way in is
the Jobs tab (answers.set_answer, called from core/web/jobs.py).

What the engine may treat as true: the full master (the one-page text it
rewrites plus the cut bullets as facts for their roles) and his typed answers.
Precheck holds every saved draft to the same: the master, or an answer he typed,
labelled "from your answer" until he promotes it (scripts/resume_promote.py).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import db
from core.jobs import answers, precheck, store, untrusted
from core.jobs import config as jobs_config
from core.resume_engine import apex_domains, bridge, one_page, runner
from core.resume_engine import apex_engine as E
from core.resume_engine import resume_format as RF

log = logging.getLogger(__name__)

RECORD_VERSION = 1

KEYWORDS_SYSTEM = (
    "You read a job posting and a candidate's evidence. You list what the posting requires and mark only the "
    "requirements the evidence clearly shows the candidate has. Return ONLY valid JSON."
)
bridge.STAGE_PROMPTS[KEYWORDS_SYSTEM] = "keywords"


class TailorError(ValueError):
    """The application cannot go through the engine. The message says why."""


# ------------------------------------------------------------------ inputs


@dataclass
class Source:
    """The resume the engine rewrites, the full master behind it, and the cut lines as facts."""

    text: str
    kind: str  # "one_page" or "full"
    full: str
    facts: list[dict[str, str]]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def load_source(directory: Path | None = None) -> Source:
    d = directory or jobs_config.RESUME_DIR
    try:
        full = RF.master_format(d)
        page = one_page.load(full, d)
    except (RF.FormatMismatch, one_page.OnePageError) as exc:
        raise TailorError(str(exc)) from exc
    if page is None:
        return Source(full, "full", full, [])
    return Source(page, "one_page", full, one_page.evidence(full, page))


async def typed_answer_facts() -> list[dict[str, str]]:
    """Trilok's typed answers to resume questions, as facts for the role each was about."""
    rows = await db.fetch(
        "select question, answer, context from candidate_answers "
        "where answer is not null and context->>'kind' = 'resume' order by answered_at"
    )
    out = []
    for r in rows:
        ctx = r["context"] or {}
        out.append({"topic": str(ctx.get("role") or ""), "text": f"{r['answer']} ({r['question']})"[:600]})
    return out


def seniority(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\b(staff|principal|distinguished|lead)\b", t):
        return "staff"
    if re.search(r"\b(senior|sr\.?)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr\.?|new grad|graduate|entry|intern|associate)\b|\b(i)\b$", t):
        return "entry"
    return "mid"


def base_target(posting: dict[str, Any], facts: list[dict[str, str]]) -> dict[str, Any]:
    title = posting.get("title") or ""
    desc = untrusted.clean(posting.get("description")).text
    return {
        "title": title,
        "seniority": seniority(title),
        "sector": "software",
        "domain": apex_domains.detect(title),
        "job_desc": desc[:12000],
        "keywords": [],
        "supported": [],
        "unsupported": [],
        "facts": facts,
    }


def _shown(term: str, evidence: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", evidence.lower()))


def read_keywords(engine_llm: bridge.EngineLLM, target: dict[str, Any], evidence: str) -> dict[str, Any]:
    """The posting's requirements, split into the ones the evidence shows and the
    rest. Fails closed: anything the model does not clearly mark shown is
    unsupported (the engine reports it as a gap and never writes it in)."""
    if not target.get("job_desc"):
        terms = apex_domains.match_terms(target.get("domain"), target.get("title") or "")
        shown = [t for t in terms if _shown(t, evidence)]
        return dict(target, keywords=[{"name": t, "pct": None} for t in terms], supported=shown,
                    unsupported=[t for t in terms if t not in shown])
    raw = engine_llm(KEYWORDS_SYSTEM, f"""{E.DATA_RULE}
TARGET ROLE: {target.get('title')}

JOB POSTING:
{E._data('POSTING', target['job_desc'])}

CANDIDATE EVIDENCE (resume and the facts the candidate confirmed):
{E._data('EVIDENCE', evidence[:20000])}

List the posting's concrete requirements (skills, tools, domains, responsibilities), at most 20, each a short
noun phrase in the posting's words. For each, shown is true only when the evidence shows the candidate doing,
using or holding it, in any wording; an adjacent skill is not enough. When unsure, shown is false.

Return ONLY JSON: {{"requirements": [{{"name": "RAG", "shown": true}}]}}""",
                     max_tokens=1500, json_mode=True, effort="low")
    data = E._jload(raw)
    reqs = data.get("requirements") if isinstance(data, dict) else None
    names: list[str] = []
    supported: list[str] = []
    for r in reqs if isinstance(reqs, list) else []:
        if not isinstance(r, dict):
            continue
        name = " ".join(str(r.get("name") or "").split())[:60]
        if not name or name.lower() in (n.lower() for n in names):
            continue
        names.append(name)
        if r.get("shown") is True:
            supported.append(name)
    names = names[:20]
    supported = [s for s in supported if s in names]
    return dict(target, keywords=[{"name": n, "pct": None} for n in names], supported=supported,
                unsupported=[n for n in names if n not in supported])


# ------------------------------------------------------------------ saving


def _notes(result: dict[str, Any], questions: int, meter: bridge.Meter | None) -> str:
    changed = len(result.get("changes") or [])
    kept = len(result.get("rejected") or [])
    score = result.get("score") or {}
    judged = (f" Hiring-manager review {score.get('before')} to {score.get('after')}."
              if score.get("before") is not None else "")
    spent = f" Spent ${meter.spent_usd:.2f}." if meter else ""
    if result.get("summary_guard"):
        spent = f" {result['summary_guard']}" + spent
    first = (f"Changed: {changed} line(s) rewritten by the evidence engine, {kept} left as written "
             f"(could not verify).{judged}")
    second = (f"{questions} question(s) for you in Jobs; answer them, then ask for a revision.{spent}"
              if questions else f"No questions for you.{spent}")
    return f"{first}\n{second}"


def _record(source: Source, posting: dict[str, Any], target: dict[str, Any], result: dict[str, Any],
            questions: list[dict[str, Any]], meter: bridge.Meter | None) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "input": source.kind,
        "input_sha256": source.sha256,
        "master_sha256": one_page.master_hash(source.full),
        "posting_id": str(posting["id"]),
        "target": {k: target.get(k) for k in ("title", "seniority", "sector", "domain", "keywords",
                                                "supported", "unsupported")},
        "changes": [{"id": c.get("id"), "after": c.get("after"), "why": c.get("why")}
                    for c in result.get("changes") or []],
        "rejected": [{"id": r.get("id"), "why": r.get("why")} for r in result.get("rejected") or []],
        "questions": questions,
        "order": result.get("order") or [],
        "section_order": result.get("section_order"),
        "score": result.get("score"),
        "veto": result.get("veto"),
        "summary_guard": result.get("summary_guard"),
        "meter": meter.summary() if meter else None,
    }


def line_key(line: str) -> str:
    """The candidate_answers key for one resume line: the same line asked about by any
    run, for any application, is one question with one answer."""
    return answers.question_key("resume line " + " ".join((line or "").split()))


async def answered_lines(doc: E.Doc) -> dict[str, str]:
    """{block id: Trilok's typed answer} for every line of `doc` he has answered before."""
    blocks = [b for b in doc.blocks if b.get("text")]
    keys = {line_key(b["text"]): b["id"] for b in blocks}
    if not keys:
        return {}
    rows = await db.fetch("select key, answer from candidate_answers where key = any($1::text[]) "
                          "and answer is not null", list(keys))
    return {keys[r["key"]]: r["answer"] for r in rows}


async def _ask(app: dict[str, Any], doc: E.Doc, result: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Record the engine's questions for Trilok, one per resume line however often it
    is asked (one notification for the run). Returns (questions with their keys,
    how many wait on him)."""
    out: list[dict[str, Any]] = []
    waiting = 0
    for q in result.get("questions") or []:
        block = doc.by_id.get(q.get("id")) or {}
        role = str(block.get("group") or "").split(" | ")[0].removeprefix("### ")
        line = " ".join(str(block.get("text") or q.get("line") or "").split())
        text = f'{q["question"]} (resume line: "{line[:90]}{"..." if len(line) > 90 else ""}")'
        key = await answers.ask(text, {"kind": "resume", "company": app["company"], "role": role,
                                       "application_id": str(app["id"]), "block_id": q["id"]},
                                notify=False, key=line_key(line))
        if await db.fetchval("select answer from candidate_answers where key = $1", key) is None:
            waiting += 1
        out.append({"id": q["id"], "question": q["question"], "key": key, "role": role})
    if waiting:
        from core.surfaces.notify import notify

        await notify("job_question", f"{waiting} resume question(s) for {app['company']}",
                     "Your own numbers make the tailored resume stronger. Answer them in Jobs; "
                     "the resume is revised only from what you type there.",
                     data={"view": "jobs", "application_id": str(app["id"])})
    return out, waiting


async def _save(app: dict[str, Any], text: str, record: dict[str, Any], notes: str, summary: str) -> None:
    await store.save_materials(app["id"], {"resume": {
        "text": f"{notes}\n{text}", "at": datetime.now(UTC).isoformat(), "engine": record}})
    await store.record_event(app["id"], "draft:resume", summary)


async def _context(application_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    app = await store.get_application(application_id)
    if not app:
        raise TailorError("no application with that id")
    if not app.get("posting_id"):
        raise TailorError("this application has no posting to tailor to; the engine needs the posting text")
    posting = await store.get_posting(app["posting_id"])
    if not posting:
        raise TailorError("the posting for this application is gone")
    return app, posting


def _refused(draft: runner.Draft) -> dict[str, Any]:
    spent = draft.meter.spent_usd if draft.meter else Decimal("0")
    return {"saved": False, "refused": draft.refused, "spent_usd": str(spent),
            "cost": draft.meter.summary() if draft.meter else None}


async def optimize_application(
    application_id: str,
    *,
    cap_usd: Decimal | None = None,
    daily_cap_usd: Decimal | None = None,
    directory: Path | None = None,
    config: bridge.EngineConfig | None = None,
    complete: bridge.CompleteFn | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Draft a tailored resume for one application. Returns a summary for the agent."""
    app, posting = await _context(application_id)
    source = load_source(directory)
    typed = await answers.resume_answers()
    facts = source.facts + await typed_answer_facts()
    evidence = "\n".join([source.full, *typed])
    target = base_target(posting, facts)
    known = await answered_lines(E.Doc(source.text))  # typed for another application: not asked again
    draft = await runner.optimize(
        source.text, target, known or None, cap_usd=cap_usd, daily_cap_usd=daily_cap_usd, config=config, complete=complete,
        prepare=lambda engine_llm, t: read_keywords(engine_llm, t, evidence),
    )
    if draft.text is None or draft.result is None:
        if save:
            await store.record_event(app["id"], "draft:resume:refused", (draft.refused or "")[:500])
        return _refused(draft)
    check = precheck.resume_check(draft.text, source.full, typed)
    if not check.ok:
        # The engine's own gate should make this impossible; never save what precheck would block.
        log.error("engine draft for %s failed precheck: %s", app["company"], check.summary())
        if save:
            await store.record_event(app["id"], "draft:resume:refused", "Pre-check: " + check.summary())
        return {"saved": False, "refused": "the draft failed the pre-apply check: " + check.summary(),
                "spent_usd": str(draft.meter.spent_usd if draft.meter else 0)}
    questions: list[dict[str, Any]] = []
    waiting = 0
    if save:
        questions, waiting = await _ask(app, E.Doc(source.text), draft.result)
        record = _record(source, posting, draft.target or target, draft.result, questions, draft.meter)
        notes = _notes(draft.result, waiting, draft.meter)
        await _save(app, draft.text, record, notes, notes.replace("\n", " "))
    return {
        "saved": save,
        "application_id": str(app["id"]),
        "input": source.kind,
        "changed": len(draft.result.get("changes") or []),
        "left_as_written": len(draft.result.get("rejected") or []),
        "questions_waiting": waiting,
        "questions": [q["question"] for q in (questions or draft.result.get("questions") or [])],
        "from_your_answer": check.from_your_answer,
        "gaps": (draft.target or {}).get("unsupported", [])[:10],
        "score": draft.result.get("score"),
        "veto": draft.result.get("veto"),
        "summary_guard": draft.result.get("summary_guard"),
        "spent_usd": str(draft.meter.spent_usd if draft.meter else 0),
        "cost": draft.meter.summary() if draft.meter else None,
        "fallbacks": draft.meter.fallbacks if draft.meter else [],
        "text": draft.text,
    }


async def revise_application(
    application_id: str,
    *,
    cap_usd: Decimal | None = None,
    daily_cap_usd: Decimal | None = None,
    directory: Path | None = None,
    config: bridge.EngineConfig | None = None,
    complete: bridge.CompleteFn | None = None,
) -> dict[str, Any]:
    """Rewrite the lines Trilok answered questions about, keeping every other edit."""
    app, posting = await _context(application_id)
    resume = (app.get("materials") or {}).get("resume") or {}
    record = resume.get("engine")
    if not record:
        raise TailorError("this application has no engine draft yet; run optimize_resume first")
    source = load_source(directory)
    if source.sha256 != record.get("input_sha256"):
        raise TailorError("the master resume or its one-page cuts changed since this draft; "
                          "run optimize_resume again")
    by_block = await answered_lines(E.Doc(source.text))
    if not by_block:
        waiting = len(record.get("questions") or [])
        raise TailorError(f"no answers yet: {waiting} question(s) wait in Jobs. Answers come only from what "
                          "Trilok types there.")
    typed = await answers.resume_answers()
    target = dict(base_target(posting, source.facts + await typed_answer_facts()), **record["target"])
    draft = await runner.revise(
        source.text, target, by_block,
        kept={c["id"]: c["after"] for c in record.get("changes") or [] if c.get("after") is not None},
        prior_questions=[{"id": q["id"], "question": q["question"]} for q in record.get("questions") or []],
        order=record.get("order") or [], section_order=record.get("section_order"),
        cap_usd=cap_usd, daily_cap_usd=daily_cap_usd, config=config, complete=complete,
    )
    if draft.text is None or draft.result is None:
        await store.record_event(app["id"], "draft:resume:refused", (draft.refused or "")[:500])
        return _refused(draft)
    check = precheck.resume_check(draft.text, source.full, typed)
    if not check.ok:
        log.error("engine revision for %s failed precheck: %s", app["company"], check.summary())
        await store.record_event(app["id"], "draft:resume:refused", "Pre-check: " + check.summary())
        return {"saved": False, "refused": "the revision failed the pre-apply check: " + check.summary(),
                "spent_usd": str(draft.meter.spent_usd if draft.meter else 0)}
    left = [q for q in record.get("questions") or [] if q["id"] not in by_block]
    new_record = _record(source, posting, target, draft.result, left, draft.meter)
    notes = _notes(draft.result, len(left), draft.meter)
    await _save(app, draft.text, new_record, notes,
                f"Revised {len(draft.result.get('revised') or [])} line(s) from your answers. "
                + notes.replace("\n", " "))
    return {
        "saved": True,
        "application_id": str(app["id"]),
        "revised": draft.result.get("revised") or [],
        "answered": sorted(by_block),
        "from_your_answer": check.from_your_answer,
        "questions_waiting": len(left),
        "spent_usd": str(draft.meter.spent_usd if draft.meter else 0),
        "fallbacks": draft.meter.fallbacks if draft.meter else [],
        "text": draft.text,
    }


__all__ = [
    "KEYWORDS_SYSTEM",
    "Source",
    "TailorError",
    "base_target",
    "load_source",
    "optimize_application",
    "read_keywords",
    "revise_application",
    "seniority",
    "typed_answer_facts",
]
