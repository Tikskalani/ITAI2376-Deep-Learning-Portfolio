"""Atlas -- signals, the input.

A signal is one thing that happened in the outside world that Atlas
should know about: a commit, a PR review request, an assignment posted
to Canvas, an email (later). Connectors insert them; triage fills in
project, urgency, importance and any deadline; the senses cycle routes
them; briefs read them.

Idempotency is the whole point of the (source, external_id) unique key:
a connector can re-read the same page of events on every tick and only
the new ones land.

Urgency and importance are 0 to 3. Urgency 3 means "interrupt now";
2 means "prepare something before the next brief"; 1 and 0 hold for the
brief. Importance orders items inside the brief.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from core import db

log = logging.getLogger(__name__)

STATUSES = ("new", "triaged", "actioned", "ignored")


class Signal(BaseModel):
    """A signal as a connector produces it (before it has an id)."""

    source: str
    external_id: str
    occurred_at: datetime
    subject: str
    body: str = ""
    actor: str | None = None
    url: str | None = None
    project_id: UUID | None = None
    deadline: date | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    # core/privacy.py: a private signal never reaches a model; the connector
    # sets these from its own fields, insert_signal enforces the storage rules.
    private: bool = False
    private_rule: str | None = None


def _gated(sig: Signal) -> Signal:
    """The storage rules of the privacy gate, for every connector alike.
    Mail and calendar text is screened for patient-data patterns first. A
    private signal keeps no body at all and its subject becomes a placeholder;
    in safe-start, a mail, calendar or GitHub signal keeps sender and subject only."""
    from core import privacy

    if not sig.private and sig.source in ("gmail", "gcal"):
        hit = privacy.screen(f"{sig.subject}\n{sig.body}")
        if hit:
            sig = sig.model_copy(update={"private": True, "private_rule": hit.label})
    if sig.private:
        label = sig.private_rule or "private"
        keep = {k: sig.raw[k] for k in ("type", "thread_id") if k in sig.raw}
        return sig.model_copy(update={"subject": privacy.placeholder(label), "body": "",
                                      "private_rule": label, "raw": {**keep, "private": True}})
    if sig.source in privacy.GATED_SOURCES and not privacy.mail_model_reads():
        return sig.model_copy(update={"body": ""})
    return sig


async def insert_signal(sig: Signal) -> UUID | None:
    """Insert one signal. Returns its id, or None if it was already there.
    The privacy gate's storage rules apply first (_gated)."""
    sig = _gated(sig)
    occurred = sig.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    row = await db.fetchrow(
        """
        insert into signals
          (source, external_id, occurred_at, actor, subject, body, url,
           project_id, deadline, raw, private, private_rule)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        on conflict (source, external_id) do nothing
        returning id
        """,
        sig.source,
        sig.external_id,
        occurred,
        sig.actor,
        sig.subject[:500],
        sig.body[:20000],
        sig.url,
        sig.project_id,
        sig.deadline,
        sig.raw,
        sig.private,
        sig.private_rule,
    )
    if row is None:
        return None
    if sig.project_id is not None:
        await db.execute(
            """
            update projects
            set last_signal_at = greatest(coalesce(last_signal_at, $2), $2),
                updated_at = now()
            where id = $1
            """,
            sig.project_id,
            occurred,
        )
    return row["id"]


async def insert_signals(sigs: list[Signal]) -> int:
    """Insert many; returns how many were new."""
    new = 0
    for s in sigs:
        if await insert_signal(s) is not None:
            new += 1
    return new


async def get_signal(signal_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow("select * from signals where id = $1", signal_id)
    return dict(row) if row else None


async def list_signals(
    status: str | None = "new",
    *,
    limit: int = 50,
    since: datetime | None = None,
    project_id: UUID | None = None,
    min_urgency: int | None = None,
) -> list[dict[str, Any]]:
    """Signals, oldest first, filtered. status=None returns every status."""
    clauses: list[str] = []
    args: list[Any] = []
    if status is not None:
        args.append(status)
        clauses.append(f"triage_status = ${len(args)}")
    if since is not None:
        args.append(since)
        clauses.append(f"occurred_at >= ${len(args)}")
    if project_id is not None:
        args.append(project_id)
        clauses.append(f"project_id = ${len(args)}")
    if min_urgency is not None:
        args.append(min_urgency)
        clauses.append(f"urgency >= ${len(args)}")
    args.append(limit)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = await db.fetch(
        f"select * from signals {where} order by occurred_at asc limit ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]


async def set_triage(
    signal_id: UUID,
    *,
    urgency: int,
    importance: int,
    project_id: UUID | None = None,
    notes: str | None = None,
    deadline: date | None = None,
    status: str = "triaged",
) -> None:
    """Record triage's verdict on one signal.

    project_id None leaves an existing link alone (connectors often know
    the project better than the model does); a value overrides it.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    await db.execute(
        """
        update signals
        set urgency = $2,
            importance = $3,
            project_id = coalesce($4, project_id),
            triage_notes = $5,
            deadline = coalesce($6, deadline),
            triage_status = $7,
            triaged_at = now()
        where id = $1
        """,
        signal_id,
        max(0, min(3, int(urgency))),
        max(0, min(3, int(importance))),
        project_id,
        notes,
        deadline,
        status,
    )


async def set_status(signal_ids: list[UUID], status: str) -> int:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if not signal_ids:
        return 0
    res = await db.execute(
        "update signals set triage_status = $2 where id = any($1)",
        signal_ids,
        status,
    )
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def signals_for_brief(
    hours: int = 24, *, limit: int = 40
) -> list[dict[str, Any]]:
    """What arrived in the window, most important first, for a brief.

    Includes triaged and actioned signals (both are things that happened);
    excludes ignored ones and anything triage has not seen yet.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await db.fetch(
        """
        select s.*, p.slug as project_slug, p.name as project_name
        from signals s
        left join projects p on p.id = s.project_id
        where s.occurred_at >= $1
          and s.triage_status in ('triaged', 'actioned')
        order by s.importance desc, s.urgency desc, s.occurred_at desc
        limit $2
        """,
        since,
        limit,
    )
    return [dict(r) for r in rows]


def format_brief_block(rows: list[dict[str, Any]]) -> str:
    """Render brief signals as compact lines the CEO can lead with."""
    if not rows:
        return "Nothing new arrived."
    lines = []
    for r in rows:
        proj = r.get("project_slug") or "unlinked"
        when = r["occurred_at"].astimezone(UTC).strftime("%m-%d %H:%M")
        flag = "!!" if r["urgency"] >= 3 else "!" if r["urgency"] == 2 else ""
        due = f" due {r['deadline'].isoformat()}" if r.get("deadline") else ""
        gist = r.get("triage_notes") or r["subject"]
        lines.append(f"- [{proj}] {when} {flag}{gist}{due} ({r['source']})")
    return "\n".join(lines)


async def brief_block(hours: int = 24) -> str:
    """The signals a brief's model may read (core/privacy.py). The rest are
    counted by code, in the line briefs append after the model's text."""
    from core import privacy

    return format_brief_block(privacy.visible(await signals_for_brief(hours)))


async def private_line(hours: int = 24) -> str:
    """Counts of the window's signals no model read, per rule label. Appended by
    code to full and quiet briefs; never an address, a subject or a body."""
    from core import privacy

    return privacy.brief_line(await signals_for_brief(hours, limit=500))


async def count_by_status() -> dict[str, int]:
    rows = await db.fetch(
        "select triage_status, count(*) as n from signals group by triage_status"
    )
    return {r["triage_status"]: int(r["n"]) for r in rows}


__all__ = [
    "STATUSES",
    "Signal",
    "insert_signal",
    "insert_signals",
    "get_signal",
    "list_signals",
    "set_triage",
    "set_status",
    "signals_for_brief",
    "format_brief_block",
    "brief_block",
    "private_line",
    "count_by_status",
]
