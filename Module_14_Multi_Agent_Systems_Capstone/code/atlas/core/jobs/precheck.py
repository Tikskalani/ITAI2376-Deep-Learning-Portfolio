"""The check before every submit (auto-apply PR D).

Deterministic, no model call, and it fails closed: anything it cannot
confirm is a problem. It catches the failures the research found most
often in automated applications:

  - the cover note names the wrong company or no role, or another
    company's name leaked in from an earlier draft;
  - leftover placeholders ({{company}}, [number?], TODO, <role>);
  - a number, employer, school or project in the tailored resume that the
    master resume does not have (the agent must never invent experience).
    The one exception is a fact Trilok typed himself as the answer to a
    resume question (core/jobs/answers.py, context kind "resume"): it is
    allowed and labelled "from your answer" so he sees it before applying,
    until he promotes it into the master (scripts/resume_promote.py);
  - a resume that does not fit one page, or a filename a recruiter would
    not want to see;
  - a posting that has already closed.

check() is the pure part; run() gathers what it needs and adds liveness.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.jobs import resume_pdf, store

_PLACEHOLDERS = (
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"\[[^\]\n]{0,40}\?\]"),  # [number?], [add metric?]
    re.compile(r"\[(?:company|role|title|name|position|hiring manager|team)[^\]\n]{0,30}\]", re.I),
    re.compile(r"<(?:company|role|title|name|position|hiring manager|team)[^>\n]{0,30}>", re.I),
    re.compile(r"\b(?:TODO|TBD|FIXME|XXX|lorem ipsum)\b"),
    re.compile(r"\bX{2,}%"),
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Words that say nothing about which role it is.
_GENERIC = {
    "and", "the", "for", "with", "senior", "junior", "staff", "principal", "lead", "remote",
    "hybrid", "onsite", "new", "grad", "intern", "contract", "full", "time", "part",
}
_ROMAN = {"i", "ii", "iii", "iv", "v"}


@dataclass
class Result:
    problems: list[str] = field(default_factory=list)
    # Claims the master lacks but an answer Trilok typed backs: allowed, shown to him.
    from_your_answer: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return "; ".join(self.problems)


def _words(text: str) -> str:
    """Normalized text padded with spaces, so ' x ' finds whole words."""
    return f" {store.norm(text)} "


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text or "")}


def _resume_body(resume: resume_pdf.Resume) -> str:
    """Everything printed below the contact line."""
    parts: list[str] = []
    for s in resume.sections:
        parts += s.paragraphs + s.bullets
        for e in s.entries:
            parts += [e.left, e.right, e.subtitle, *e.bullets]
    return "\n".join(parts)


def _organizations(resume: resume_pdf.Resume) -> list[str]:
    """The employer, school or project each '### ' line names (before the city)."""
    out = []
    for s in resume.sections:
        for e in s.entries:
            org = e.left.split(",")[0].strip()
            if org:
                out.append(org)
    return out


def _role_named(title: str, cover: str) -> bool:
    words = _words(cover)
    if _words(title).strip() and _words(title) in words:
        return True
    tokens = [t for t in store.norm(title).split() if len(t) > 2 and t not in _GENERIC and t not in _ROMAN]
    if not tokens:
        return True
    found = sum(f" {t} " in words for t in tokens)
    return found / len(tokens) >= 0.6


def _claims(resume: resume_pdf.Resume, master: str, answers: Sequence[str]) -> tuple[list[str], list[str]]:
    """(invented, from_your_answer): numbers and organizations in the resume
    that the master lacks, split by whether one of Trilok's typed answers has them."""
    master_words = _words(master)
    answer_text = "\n".join(answers)
    answer_words, answer_numbers = _words(answer_text), _numbers(answer_text)
    new_numbers = sorted(_numbers(_resume_body(resume)) - _numbers(master), key=lambda s: (len(s), s))
    invented = [n for n in new_numbers if n not in answer_numbers]
    backed = [n for n in new_numbers if n in answer_numbers]
    orgs_invented: list[str] = []
    orgs_backed: list[str] = []
    for org in _organizations(resume):
        if _words(org).strip() and _words(org) not in master_words:
            (orgs_backed if _words(org) in answer_words else orgs_invented).append(org)
    problems = []
    if invented:
        problems.append("the resume has numbers the master resume does not: " + ", ".join(invented[:8]))
    problems += [f"the resume names {org!r}, which the master resume does not" for org in orgs_invented]
    return problems, [f"{x} (from your answer)" for x in backed + orgs_backed]


def answer_backed(resume_text: str, master: str, answers: Sequence[str]) -> list[str]:
    """The claims in a tailored resume that only Trilok's typed answers back,
    labelled for the pre-apply view. Empty when the resume does not parse."""
    try:
        resume = resume_pdf.parse(resume_text or "")
    except resume_pdf.ResumeFormatError:
        return []
    return _claims(resume, master, answers)[1] if master.strip() else []


def resume_check(resume_text: str, master: str, answers: Sequence[str] = ()) -> Result:
    """The resume half of check(), for a draft before it is saved: it parses,
    fits one page and claims nothing the master or a typed answer lacks."""
    r = Result()
    if not (master or "").strip():
        r.problems.append("no master resume in data/resume/ to check the tailored one against")
        return r
    try:
        resume = resume_pdf.parse(resume_text or "")
    except resume_pdf.ResumeFormatError as exc:
        r.problems.append(f"the tailored resume does not parse: {exc}")
        return r
    r.problems, r.from_your_answer = _claims(resume, master, answers)
    rendered = resume_pdf.render(resume)
    if not rendered.fits:
        r.problems.append(f"the resume runs to {rendered.pages} pages")
    return r


def check(
    *,
    company: str,
    title: str,
    resume_text: str,
    cover: str,
    master: str,
    other_companies: list[str] | tuple[str, ...] = (),
    answers: Sequence[str] = (),
) -> Result:
    """Every problem with these materials for this posting. Empty means go.
    answers: Trilok's own typed answers to resume questions; a number or
    organization only they back is allowed and listed in from_your_answer."""
    r = Result()
    if not (master or "").strip():
        r.problems.append("no master resume in data/resume/ to check the tailored one against")
    if not (cover or "").strip():
        r.problems.append("the cover note is empty")
    try:
        resume = resume_pdf.parse(resume_text or "")
    except resume_pdf.ResumeFormatError as exc:
        r.problems.append(f"the tailored resume does not parse: {exc}")
        return r

    printed = resume_text[resume_text.find("# "):] if "# " in resume_text else resume_text
    for label, text in (("cover note", cover), ("resume", printed)):
        for pat in _PLACEHOLDERS:
            m = pat.search(text or "")
            if m:
                r.problems.append(f"the {label} still has a placeholder: {m.group(0)!r}")
                break

    if cover and _words(company).strip() and _words(company) not in _words(cover):
        r.problems.append(f"the cover note never names {company}")
    if cover and not _role_named(title, cover):
        r.problems.append(f"the cover note does not name the role ({title})")

    master_words = _words(master)
    target = store.norm(company)
    for other in sorted(set(other_companies)):
        n = store.norm(other)
        if not n or n == target or len(n) < 3 or f" {n} " in master_words:
            continue  # a former employer may appear; the target obviously may
        pat = re.compile(rf"(?<![\w-]){re.escape(other.strip())}(?![\w-])")
        for label, text in (("cover note", cover), ("resume", printed)):
            if pat.search(text or ""):
                r.problems.append(f"the {label} names another company: {other}")

    if master.strip():
        problems, backed = _claims(resume, master, answers)
        r.problems += problems
        r.from_your_answer += backed

    rendered = resume_pdf.render(resume)
    if not rendered.fits:
        r.problems.append(f"the resume runs to {rendered.pages} pages")
    name = resume.name.strip()
    if not name or store.norm(name) in ("full name", "name", "your name"):
        r.problems.append("the resume has no real name on it")
    fname = resume_pdf.filename(resume, company)
    if not (fname.startswith(name) and " - Resume - " in fname and fname.endswith(".pdf")):
        r.problems.append(f"the resume file would be named {fname!r}")
    return r


async def run(
    posting: dict[str, Any],
    materials: dict[str, Any],
    *,
    master: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Result:
    """check() plus: the posting must still be open on its board."""
    from core import db
    from core.jobs import answers as answers_mod
    from core.jobs import config as jobs_config
    from core.jobs.discovery import is_live

    if master is None:
        master = jobs_config.resume_text()
    others = [r["company"] for r in await db.fetch(
        "select distinct company from job_postings where company <> $1", posting["company"]
    )]
    result = check(
        company=posting["company"],
        title=posting["title"],
        resume_text=(materials.get("resume") or {}).get("text") or "",
        cover=(materials.get("cover") or {}).get("text") or "",
        master=master,
        other_companies=others,
        answers=await answers_mod.resume_answers(),
    )
    live = await is_live(posting, client)
    if live is False:
        result.problems.insert(0, "the posting has closed")
    elif live is None:
        result.problems.append("could not confirm the posting is still open")
    return result


__all__ = ["Result", "answer_backed", "check", "resume_check", "run"]
