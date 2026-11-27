"""The answers Trilok gives once so Atlas can fill job applications.

Two kinds of question:

  standard  the ones nearly every form asks (contact, work authorization,
            sponsorship, start date, the voluntary EEO set). Defined here,
            filled in once from the Jobs tab.
  learned   any other factual question a form asks ("Have you worked with
            Kubernetes in production?"). Open prompts like "Why this
            company?" are not answers; the career agent writes those per
            application. The first time Atlas meets a learned question it
            calls ask(): the question is recorded unanswered, Trilok gets
            one notification, and the application waits. His answer is
            reused for every later form that asks the same thing.

Answers are personal, so they live in Postgres (candidate_answers), never in
config/jobs.yaml, and nothing here logs an answer's value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core import db

log = logging.getLogger(__name__)

DECLINE = "Decline to self-identify"
YES_NO = ("Yes", "No")
MAX_ANSWER = 2000
MAX_QUESTION = 500
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    group: str
    kind: str = "text"  # text | email | phone | url | choice
    choices: tuple[str, ...] = ()
    required: bool = True
    default: str | None = None
    help: str = ""
    placeholder: str = ""
    aliases: tuple[str, ...] = field(default=())


STANDARD: tuple[Question, ...] = (
    Question("full_name", "Full legal name", "Contact", placeholder="As it should appear on applications"),
    Question("email", "Email", "Contact", kind="email"),
    Question("phone", "Phone", "Contact", kind="phone", placeholder="+1 713 555 0100"),
    Question("location", "City and state", "Contact", placeholder="Houston, TX"),
    Question("linkedin_url", "LinkedIn", "Contact", kind="url", required=False,
             placeholder="https://www.linkedin.com/in/…"),
    Question("github_url", "GitHub", "Contact", kind="url", required=False, placeholder="https://github.com/…"),
    Question("portfolio_url", "Portfolio or website", "Contact", kind="url", required=False),
    Question("work_authorized_us", "Authorized to work in the US?", "Eligibility", kind="choice", choices=YES_NO,
             aliases=("legally authorized to work in the united states",
                      "are you authorized to work in the us")),
    Question("needs_sponsorship", "Will you need visa sponsorship, now or later?", "Eligibility", kind="choice",
             choices=YES_NO,
             aliases=("require sponsorship", "will you now or in the future require sponsorship")),
    Question("earliest_start", "Earliest start date", "Eligibility", placeholder="January 2027"),
    Question("relocate", "Willing to relocate?", "Eligibility", kind="choice",
             choices=("Yes", "No", "For the right role")),
    Question("work_mode", "Work setting you want", "Eligibility", kind="choice",
             choices=("Remote", "Hybrid", "On-site", "Any")),
    Question("salary_expectation", "Salary expectation", "Eligibility", required=False,
             help="Only used when a form requires a number. Leave blank and Atlas will pause instead."),
    Question("gender", "Gender", "Voluntary (EEO)", kind="choice", choices=("Male", "Female", "Non-binary", DECLINE),
             default=DECLINE, required=False),
    Question("hispanic_latino", "Hispanic or Latino?", "Voluntary (EEO)", kind="choice", choices=(*YES_NO, DECLINE),
             default=DECLINE, required=False),
    Question("race", "Race", "Voluntary (EEO)", kind="choice",
             choices=("American Indian or Alaska Native", "Asian", "Black or African American",
                      "Native Hawaiian or Other Pacific Islander", "White", "Two or more races", DECLINE),
             default=DECLINE, required=False),
    Question("veteran_status", "Protected veteran?", "Voluntary (EEO)", kind="choice",
             choices=("I am not a protected veteran", "I am a protected veteran", DECLINE), default=DECLINE, required=False),
    Question("disability_status", "Disability", "Voluntary (EEO)", kind="choice",
             choices=("No, I do not have a disability", "Yes, I have a disability", DECLINE), default=DECLINE, required=False),
)

BY_KEY: dict[str, Question] = {q.key: q for q in STANDARD}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9 ().-]{7,20}$")
_URL = re.compile(r"^https?://[^\s]+\.[^\s]+$", re.I)
_REQUIRED_MARK = re.compile(r"\s*(\*|\(required\)|\(optional\))\s*$", re.I)


class AnswerError(ValueError):
    """An answer that does not fit its question. The message is safe to show."""


def question_key(question: str) -> str:
    """A stable key for a free-text form question: lower case, required
    markers and punctuation dropped, whitespace collapsed. Two forms that ask
    the same thing in the same words share an answer."""
    text = _REQUIRED_MARK.sub("", (question or "").strip())
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return "q:" + " ".join(words)[:200] if words else ""


def standard_for(question: str) -> Question | None:
    """The standard question a form's wording matches by label or alias phrase.
    Fuzzy matching is the submitter's job; this only catches the common
    phrasings so they never become learned duplicates."""
    key = question_key(question)[2:]
    for q in STANDARD:
        if key == question_key(q.label)[2:] or any(a in key for a in q.aliases):
            return q
    return None


def validate(key: str, answer: str) -> str:
    """The cleaned answer, or AnswerError. Empty means "clear it"."""
    value = (answer or "").strip()
    if len(value) > MAX_ANSWER:
        raise AnswerError(f"Keep it under {MAX_ANSWER} characters.")
    q = BY_KEY.get(key)
    if q is None or not value:
        return value
    if q.kind == "choice" and value not in q.choices:
        raise AnswerError(f"Pick one of: {', '.join(q.choices)}.")
    if q.kind == "email" and not _EMAIL.match(value):
        raise AnswerError("That does not look like an email address.")
    if q.kind == "phone" and not _PHONE.match(value):
        raise AnswerError("Use digits, spaces, dashes or a leading +.")
    if q.kind == "url" and not _URL.match(value):
        raise AnswerError("Use a full link starting with https://.")
    return value


async def _rows() -> dict[str, dict[str, Any]]:
    rows = await db.fetch(
        "select key, question, answer, standard, asked_count, last_asked_at, answered_at, context "
        "from candidate_answers"
    )
    return {r["key"]: dict(r) for r in rows}


def _effective(q: Question, row: dict[str, Any] | None) -> str | None:
    if row is not None and row["answer"] is not None:
        return row["answer"]
    return q.default


async def overview() -> dict[str, Any]:
    """Everything the Jobs tab shows: standard questions with their answers,
    learned answers, and the questions waiting on Trilok."""
    rows = await _rows()
    standard = []
    for q in STANDARD:
        standard.append({
            "key": q.key, "label": q.label, "group": q.group, "kind": q.kind, "choices": list(q.choices),
            "required": q.required, "help": q.help, "placeholder": q.placeholder,
            "answer": _effective(q, rows.get(q.key)),
        })
    learned: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    def recency(r: dict[str, Any]) -> datetime:
        return r["last_asked_at"] or r["answered_at"] or _EPOCH

    for r in sorted((r for r in rows.values() if not r["standard"]), key=recency, reverse=True):
        item = {"key": r["key"], "question": r["question"], "answer": r["answer"],
                "asked_count": r["asked_count"], "last_asked_at": r["last_asked_at"], "context": r["context"]}
        (learned if r["answer"] is not None else pending).append(item)
    missing = [s["key"] for s in standard if s["required"] and not s["answer"]]
    return {"standard": standard, "learned": learned, "pending": pending, "missing_required": missing}


async def set_answer(key: str, answer: str, question: str | None = None) -> dict[str, Any]:
    """Save an answer. A standard key is validated against its question; any
    other key must already be a recorded question, or come with its text.
    An empty answer clears a standard answer (back to its default) and puts
    a learned question back to waiting."""
    q = BY_KEY.get(key)
    value = validate(key, answer)
    if q is None:
        if not key.startswith("q:"):
            raise AnswerError("Unknown question.")
        existing = await db.fetchval("select question from candidate_answers where key = $1", key)
        text = (question or existing or "").strip()[:MAX_QUESTION]
        if not text:
            raise AnswerError("Unknown question.")
    else:
        text = q.label
    row = await db.fetchrow(
        """
        insert into candidate_answers (key, question, answer, standard, answered_at)
        values ($1, $2, $3, $4, case when $3::text is null then null else now() end)
        on conflict (key) do update
          set answer = excluded.answer,
              answered_at = excluded.answered_at,
              updated_at = now()
        returning key, question, answer, standard, answered_at
        """,
        key, text, value or None, q is not None,
    )
    log.info("candidate answer %s %s", key, "saved" if value else "cleared")
    return dict(row)


async def forget(key: str) -> bool:
    """Drop a learned question and its answer. Standard questions are cleared
    with set_answer(key, '') instead."""
    if key in BY_KEY:
        raise AnswerError("Standard questions are cleared, not deleted.")
    res = await db.execute("delete from candidate_answers where key = $1 and not standard", key)
    return res.endswith("1")


async def answer_for(question: str) -> str | None:
    """The saved answer for a form question, or None if Atlas has not got
    one. Standard wording resolves to the standard answer (or its default)."""
    q = standard_for(question)
    key = q.key if q else question_key(question)
    if not key:
        return None
    row = await db.fetchrow("select answer from candidate_answers where key = $1", key)
    if q is not None:
        return _effective(q, dict(row) if row else None)
    return row["answer"] if row else None


async def ask(
    question: str,
    context: dict[str, Any] | None = None,
    *,
    notify: bool = True,
    key: str | None = None,
) -> str:
    """Record a form question Atlas cannot answer and return its key. The
    first time a question is seen, Trilok gets one notification; asking
    again only counts it. Standard wording is never recorded as learned.
    notify=False records it silently, for a caller that sends one
    notification for a batch (the resume engine's questions). key: the
    caller's own stable key (it must start with "q:"), for a question whose
    wording varies but whose answer does not (one resume line, asked by
    different engine runs); the first wording recorded is the one shown."""
    if key is None:
        q = standard_for(question)
        if q is not None:
            return q.key
        key = question_key(question)
    elif not key.startswith("q:"):
        raise AnswerError("A learned question's key starts with q:.")
    if not key:
        raise AnswerError("Empty question.")
    text = question.strip()[:MAX_QUESTION]
    row = await db.fetchrow(
        """
        insert into candidate_answers (key, question, asked_count, first_asked_at, last_asked_at, context)
        values ($1, $2, 1, now(), now(), $3)
        on conflict (key) do update
          set asked_count = candidate_answers.asked_count + 1,
              last_asked_at = now(),
              context = excluded.context,
              updated_at = now()
        returning asked_count, answer
        """,
        key, text, context or {},
    )
    if notify and row["asked_count"] == 1 and row["answer"] is None:
        from core.surfaces.notify import notify as send

        where = (context or {}).get("company")
        await send(
            "job_question",
            "An application needs one answer",
            f"{text}" + (f"\n\nAsked by {where}." if where else "") + "\n\nAnswer it once in Jobs; Atlas reuses it.",
            data={"key": key, "view": "jobs"},
        )
    return key


async def resume_answers() -> list[str]:
    """Trilok's answers to the resume engine's questions, as "question answer"
    text: evidence precheck accepts beside the master, and facts the engine
    may use. Every answer in this table is typed by him in the Jobs tab
    (set_answer is called from core/web/jobs.py only; tests/test_resume_tools.py
    checks that no agent or engine code calls it), so none is model-written."""
    rows = await db.fetch(
        "select question, answer from candidate_answers "
        "where answer is not null and context->>'kind' = 'resume' order by answered_at"
    )
    return [f"{r['question']} {r['answer']}" for r in rows]


async def missing_required() -> list[str]:
    """Standard questions that must be answered before Atlas fills a form."""
    return (await overview())["missing_required"]


async def form_profile() -> dict[str, str]:
    """Every usable answer, standard and learned, keyed for the submitter."""
    rows = await _rows()
    out: dict[str, str] = {}
    for q in STANDARD:
        value = _effective(q, rows.get(q.key))
        if value:
            out[q.key] = value
    for r in rows.values():
        if not r["standard"] and r["answer"] is not None:
            out[r["key"]] = r["answer"]
    return out
