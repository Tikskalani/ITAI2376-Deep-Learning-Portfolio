"""Atlas -- projects, the unit of organization.

One row per thing Trilok is working on: a client engagement, his own
business, a course, the job search, a family project, Atlas itself. Every signal,
deadline and brief item hangs off a project. The table replaces the
priority list that used to be hardcoded into the CEO prompt: the CEO
reads `projects_block()` at the top of every run instead.

Status: active projects get loop attention and brief space; waiting ones
are blocked on someone else and only surface when a signal arrives;
parked and done ones are silent. Priority 1 is highest.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from core import db

log = logging.getLogger(__name__)

KINDS = ("client", "own", "hobby", "personal", "college", "atlas")
STATUSES = ("active", "waiting", "parked", "done")


class Project(BaseModel):
    """A project row as the rest of Atlas sees it."""

    id: UUID
    slug: str
    name: str
    kind: str
    status: str
    priority: int
    objective: str | None = None
    next_action: str | None = None
    next_action_due: date | None = None
    owner_agent: str = "ceo"
    repos: list[str] = Field(default_factory=list)
    last_signal_at: datetime | None = None
    last_touched_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _row(row: Any) -> Project:
    return Project(**dict(row))


async def upsert_project(
    slug: str,
    name: str,
    *,
    kind: str = "client",
    status: str = "active",
    priority: int = 3,
    objective: str | None = None,
    next_action: str | None = None,
    next_action_due: date | None = None,
    owner_agent: str = "ceo",
    repos: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Project:
    """Create a project or update the given fields of an existing one.

    Keyed on slug. A re-seed with the same slug updates name, kind,
    priority, objective, owner and repos but never clears next_action or
    the touch timestamps, which the loop owns.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if not 1 <= priority <= 5:
        raise ValueError(f"priority must be 1 to 5, got {priority}")
    row = await db.fetchrow(
        """
        insert into projects
          (slug, name, kind, status, priority, objective, next_action,
           next_action_due, owner_agent, repos, metadata)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        on conflict (slug) do update set
          name = excluded.name,
          kind = excluded.kind,
          status = excluded.status,
          priority = excluded.priority,
          objective = coalesce(excluded.objective, projects.objective),
          next_action = coalesce(excluded.next_action, projects.next_action),
          next_action_due = coalesce(excluded.next_action_due, projects.next_action_due),
          owner_agent = excluded.owner_agent,
          repos = excluded.repos,
          metadata = projects.metadata || excluded.metadata,
          updated_at = now()
        returning *
        """,
        slug,
        name,
        kind,
        status,
        priority,
        objective,
        next_action,
        next_action_due,
        owner_agent,
        [r.strip().lower() for r in (repos or []) if r.strip()],
        metadata or {},
    )
    return _row(row)


async def get_project(slug: str) -> Project | None:
    row = await db.fetchrow("select * from projects where slug = $1", slug)
    return _row(row) if row else None


async def get_project_by_id(project_id: UUID) -> Project | None:
    row = await db.fetchrow("select * from projects where id = $1", project_id)
    return _row(row) if row else None


async def list_projects(
    status: str | None = "active", *, kinds: tuple[str, ...] | None = None
) -> list[Project]:
    """Projects ordered by priority then name. status=None returns all."""
    clauses: list[str] = []
    args: list[Any] = []
    if status is not None:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    if kinds:
        args.append(list(kinds))
        clauses.append(f"kind = any(${len(args)})")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = await db.fetch(
        f"select * from projects {where} order by priority, name", *args
    )
    return [_row(r) for r in rows]


async def project_for_repo(repo: str) -> Project | None:
    """The project that owns a GitHub repo ('owner/name', case-insensitive)."""
    row = await db.fetchrow(
        "select * from projects where $1 = any(repos) order by priority limit 1",
        repo.strip().lower(),
    )
    return _row(row) if row else None


async def touch_project(
    project_id: UUID, *, signal_at: datetime | None = None
) -> None:
    """Record that something happened on a project.

    signal_at updates last_signal_at (a connector saw activity);
    last_touched_at always moves to now (Atlas or Trilok did something).
    """
    await db.execute(
        """
        update projects
        set last_touched_at = now(),
            last_signal_at = coalesce($2, last_signal_at),
            updated_at = now()
        where id = $1
        """,
        project_id,
        signal_at,
    )


async def set_next_action(
    project_id: UUID, next_action: str | None, due: date | None = None
) -> None:
    await db.execute(
        """
        update projects
        set next_action = $2, next_action_due = $3,
            last_touched_at = now(), updated_at = now()
        where id = $1
        """,
        project_id,
        next_action,
        due,
    )


async def set_status(slug: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    await db.execute(
        "update projects set status = $2, updated_at = now() where slug = $1",
        slug,
        status,
    )


async def delete_project(slug: str) -> bool:
    res = await db.execute("delete from projects where slug = $1", slug)
    return res.endswith("1")


def format_projects_block(projects: list[Project]) -> str:
    """The compact, prompt-cacheable list of projects for triage and the CEO.

    One line per project. Stable ordering (priority, name) so the block
    only changes when a project changes, which keeps the cache warm.
    """
    if not projects:
        return "(no projects on record)"
    lines = []
    for p in projects:
        bits = [f"p{p.priority}", p.kind, p.status]
        if p.repos:
            bits.append("repos: " + ", ".join(p.repos))
        line = f"- {p.slug}: {p.name} [{' | '.join(bits)}]"
        if p.objective:
            line += f"\n    objective: {p.objective}"
        if p.next_action:
            due = f" (due {p.next_action_due.isoformat()})" if p.next_action_due else ""
            line += f"\n    next: {p.next_action}{due}"
        lines.append(line)
    return "\n".join(lines)


async def projects_block(status: str | None = "active") -> str:
    return format_projects_block(await list_projects(status))


__all__ = [
    "KINDS",
    "STATUSES",
    "Project",
    "upsert_project",
    "get_project",
    "get_project_by_id",
    "list_projects",
    "project_for_repo",
    "touch_project",
    "set_next_action",
    "set_status",
    "delete_project",
    "format_projects_block",
    "projects_block",
]
