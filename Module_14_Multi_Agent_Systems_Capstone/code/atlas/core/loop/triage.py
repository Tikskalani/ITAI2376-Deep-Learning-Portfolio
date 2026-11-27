"""Atlas -- signal triage on Haiku.

Every executive tick, the signals the connectors wrote since the last
tick go through one batched Haiku call that returns, per signal: the
project it belongs to, urgency and importance on a 0 to 3 scale, any
hard date it carries, and a one-line gist in plain English. The gist is
what the brief shows and what the PWA shows next to the verdict, so the
reasoning is always visible.

Cost: the rubric and the project list are the system prompt and are
prompt-cached; a batch of twenty signals is a few thousand input tokens
on Haiku, well under a cent. Usage is booked through llm.complete.

The privacy gate (core/privacy.py): a signal no model may see (private,
or mail, calendar or GitHub while mail model reads are off) never goes
into the batch. It is scored here by rules: urgency 1, importance from
the known-sender tier (never for a private one: exclusion beats the
known-people list), deadlines from the connector's own date or a date in
a subject the gate let it keep, and a deadline title that says nothing.

Failure modes, all non-fatal: an empty or unparseable reply leaves the
batch as `new` so the next tick retries; a signal the model skipped is
marked triaged with zeros and a note so it cannot loop forever.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

from core import db, llm, privacy
from core.config import settings
from core.operations import add_deadline
from core.projects import format_projects_block, list_projects
from core.signals import list_signals, set_triage

log = logging.getLogger(__name__)

RUBRIC = """You are the triage step of Atlas, Trilok Kalani's personal operating system.
You read inbound signals (GitHub events, Canvas assignments, email later) and decide,
for each one, where it belongs and how much it matters. Trilok is a software engineer
with client projects, a day job, college coursework, and an active job search. He is
the only user. Be terse and literal; never invent facts.

For every signal return one JSON object with these keys:
  id          the signal id, copied exactly
  project     the slug of the matching project from the list below, or null
  urgency     0 none, 1 this week, 2 prepare something before the next brief,
              3 interrupt Trilok now (a hard deadline inside 24 hours, production
              or CI broken on a client's main branch, a client or recruiter waiting
              on a reply he owes, a grade or assignment surprise due tomorrow)
  importance  0 trivia, 1 nice to know, 2 matters this week, 3 affects money,
              a client relationship, a grade, or the job search
  deadline    an ISO date (YYYY-MM-DD) when the signal carries a hard date, else null
  gist        one plain sentence, at most 140 characters, that says what happened
              and what Trilok might do about it, in his voice (no exclamation
              marks, no "please note")
  ignore      true only for pure noise (bot chatter, his own routine pushes with
              nothing to act on, duplicate notifications)

Rules:
- A signal already linked to a project keeps that project unless it is clearly wrong.
- His own commits and merges are urgency 0 unless CI failed or a client is waiting.
- Review requests, questions addressed to him, failed CI on main, and assignments
  due within 48 hours are at least urgency 2.
- Do not raise urgency to 3 for more than two signals in a batch.
- Return the verdicts as the JSON object the schema describes, one per signal."""


def _signal_for_prompt(row: dict[str, Any], slug_by_id: dict[Any, str]) -> dict[str, Any]:
    occurred: datetime = row["occurred_at"]
    return {
        "id": str(row["id"]),
        "source": row["source"],
        "when": occurred.isoformat(timespec="minutes"),
        "actor": row.get("actor"),
        "subject": row["subject"][:300],
        "body": (row.get("body") or "")[:600],
        "url": row.get("url"),
        "project": slug_by_id.get(row.get("project_id")),
        "deadline": row["deadline"].isoformat() if row.get("deadline") else None,
        "known": ((row.get("raw") or {}).get("known") if isinstance(row.get("raw"), dict) else None),
    }


_ARRAY = re.compile(r"\[\s*\{.*\}\s*\]", re.DOTALL)

# The reply is constrained to this shape (structured outputs), so a batch
# never fails on a stray code fence or a trailing sentence. No minimum or
# maximum: the API refuses them (400 on every tick until 2026-10-23);
# _int clamps urgency and importance to 0..3 instead.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "project": {"type": ["string", "null"]},
                    "urgency": {"type": "integer"},
                    "importance": {"type": "integer"},
                    "deadline": {"type": ["string", "null"]},
                    "gist": {"type": "string"},
                    "ignore": {"type": "boolean"},
                },
                "required": ["id", "project", "urgency", "importance", "deadline", "gist", "ignore"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def parse_triage(text: str) -> list[dict[str, Any]]:
    """Pull the JSON array of verdicts out of a model reply.

    Tolerates a code fence and surrounding prose. Returns [] when there
    is no parseable array; a non-list or non-dict item is dropped.
    """
    if not text:
        return []
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", candidate).strip()
    for attempt in (candidate, *_ARRAY.findall(candidate)):
        try:
            data = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return []


def _int(value: Any, lo: int = 0, hi: int = 3) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def _date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def _ensure_deadline(
    row: dict[str, Any], due: date, gist: str, project_slug: str | None,
    project_id: UUID | None, urgency: int,
) -> None:
    """Create a deadlines row for a dated signal, once per signal."""
    exists = await db.fetchval(
        "select 1 from deadlines where signal_id = $1", row["id"]
    )
    if exists:
        return
    did = await add_deadline(
        title=(gist or row["subject"])[:200],
        due_date=due,
        domain=project_slug,
        priority="high" if urgency >= 2 else "normal",
        notes=row.get("url"),
        source="triage",
    )
    await db.execute(
        "update deadlines set project_id = $2, signal_id = $3 where id = $1",
        did,
        project_id,
        row["id"],
    )


_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def rule_verdict(row: dict[str, Any]) -> dict[str, Any]:
    """How a signal no model may see is scored: by rules, from local fields only."""
    from core import context_pack

    private = privacy.is_private(row)
    importance = 1
    if not private:
        raw = row.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        hint = context_pack.sender_hint(str(raw.get("from") or ""))
        if hint:
            importance = {1: 3, 2: 2}.get(int(hint.get("tier") or 3), 1)
    due = row.get("deadline")
    if due is None and not private:
        m = _DATE.search(row.get("subject") or "")
        due = _date(m.group(0)) if m else None
    why = f"private ({row.get('private_rule') or 'private'})" if private else "mail model reads are off"
    title = (row["subject"] if private else
             f"Dated {row['source']} item (not read by a model)")
    return {"urgency": 1, "importance": importance, "deadline": due,
            "notes": f"Scored by rules: {why}.", "title": title}


async def _triage_by_rules(rows: list[dict[str, Any]], out: dict[str, Any]) -> None:
    for row in rows:
        v = rule_verdict(row)
        try:
            await set_triage(row["id"], urgency=v["urgency"], importance=v["importance"],
                             notes=v["notes"], deadline=v["deadline"])
            out["triaged"] += 1
            out["by_rules"] += 1
            if v["deadline"] is not None:
                await _ensure_deadline(row, v["deadline"], v["title"], None, row.get("project_id"), 1)
                out["deadlines"] += 1
        except Exception:
            log.exception("triage: could not score signal %s by rules", row["id"])


async def triage_new_signals(batch: int | None = None) -> dict[str, Any]:
    """Triage up to `batch` new signals in one Haiku call. Never raises.

    Returns {seen, triaged, ignored, deadlines, interrupts, error}.
    """
    out: dict[str, Any] = {
        "seen": 0, "triaged": 0, "ignored": 0, "deadlines": 0, "interrupts": 0, "by_rules": 0,
        "error": None,
    }
    try:
        rows = await list_signals("new", limit=batch or settings.triage_batch)
    except Exception as exc:
        log.exception("triage: could not list signals")
        out["error"] = f"list: {exc}"
        return out
    out["seen"] = len(rows)
    blind = [r for r in rows if not privacy.model_visible(r)]
    if blind:
        await _triage_by_rules(blind, out)
    rows = privacy.visible(rows)
    if not rows:
        return out

    try:
        projects = await list_projects("active")
    except Exception as exc:
        log.exception("triage: could not list projects")
        out["error"] = f"projects: {exc}"
        return out
    slug_by_id = {p.id: p.slug for p in projects}
    id_by_slug = {p.slug: p.id for p in projects}

    from core import context_pack

    extra = context_pack.triage_block()
    system = f"{RUBRIC}\n\n{extra}\n\nProjects:\n{format_projects_block(projects)}" if extra \
        else f"{RUBRIC}\n\nProjects:\n{format_projects_block(projects)}"
    user = json.dumps([_signal_for_prompt(r, slug_by_id) for r in rows], ensure_ascii=False)
    data = await llm.complete_json(
        system=system,
        user=user,
        schema=VERDICT_SCHEMA,
        tier="scan",
        max_tokens=200 + 120 * len(rows),
        purpose="triage",
        cache_system=True,
    )
    items = data.get("verdicts") if isinstance(data, dict) else None
    verdicts = {str(v.get("id")): v for v in (items or []) if isinstance(v, dict)}
    if not verdicts:
        log.warning("triage: no verdicts in the reply; will retry")
        out["error"] = "no verdicts"
        return out

    for row in rows:
        sid = row["id"]
        v = verdicts.get(str(sid))
        try:
            if v is None:
                await set_triage(sid, urgency=0, importance=0, notes="triage: no verdict returned")
                out["triaged"] += 1
                continue
            if v.get("ignore") is True:
                await set_triage(
                    sid, urgency=0, importance=0,
                    notes=str(v.get("gist") or "noise")[:200], status="ignored",
                )
                out["ignored"] += 1
                continue
            slug = v.get("project") if isinstance(v.get("project"), str) else None
            project_id = id_by_slug.get(slug) if slug else None
            if project_id is None and row.get("project_id") is not None:
                project_id = row["project_id"]
                slug = slug_by_id.get(project_id)
            urgency = _int(v.get("urgency"))
            importance = _int(v.get("importance"))
            gist = str(v.get("gist") or row["subject"])[:200]
            due = _date(v.get("deadline")) or row.get("deadline")
            await set_triage(
                sid, urgency=urgency, importance=importance,
                project_id=project_id, notes=gist, deadline=due,
            )
            out["triaged"] += 1
            if urgency >= 3:
                out["interrupts"] += 1
            if due is not None:
                await _ensure_deadline(row, due, gist, slug, project_id, urgency)
                out["deadlines"] += 1
        except Exception:
            log.exception("triage: could not apply verdict to signal %s", sid)
    log.info(
        "triage: %d seen, %d triaged, %d ignored, %d deadlines, %d urgent",
        out["seen"], out["triaged"], out["ignored"], out["deadlines"], out["interrupts"],
    )
    return out


__all__ = ["RUBRIC", "parse_triage", "triage_new_signals"]
