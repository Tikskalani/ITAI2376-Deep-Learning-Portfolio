"""Atlas -- the MCP server (v2): Atlas as the memory of record for Claude.

One FastMCP instance, two transports:

  * stdio     scripts/mcp_server.py, for Claude Desktop on the laptop
  * HTTP      mounted at /mcp/ on the web process (core/web/api.py) behind
              ATLAS_MCP_TOKEN, so Cowork, Claude Code in the cloud, and
              the Claude mobile app reach it through the tunnel

Reads: projects and their context packs, hybrid search over facts,
lessons, episodes and signals, procedures, learnings. Writes, all
audited and none irreversible: record_outcome (what a Claude session
did on a project, written back as an episode plus optional next action),
remember_fact, log_decision, add_deadline, set_profile_note, rate_outcome.
Nothing here sends, posts or submits; those stay behind Atlas's approval cards.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_server):
    """Warm the DB pool so the first tool call is fast."""
    try:
        from core import db

        await db.get_pool()
    except Exception as exc:  # noqa: BLE001
        log.warning("atlas mcp warmup failed: %s", exc)
    yield


mcp = FastMCP(
    "atlas",
    lifespan=_lifespan,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


def _iso(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _clean(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: _iso(row.get(k)) for k in keys}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_projects(status: str = "active") -> list[dict[str, Any]]:
    """Trilok's projects with priority, status, objective and next action.
    status: active (default), waiting, parked, done, or all."""
    from core.projects import list_projects as _list

    rows = await _list(None if status == "all" else status)
    return [
        {
            "slug": p.slug, "name": p.name, "kind": p.kind, "status": p.status,
            "priority": p.priority, "objective": p.objective, "next_action": p.next_action,
            "next_action_due": _iso(p.next_action_due), "repos": p.repos,
            "last_signal_at": _iso(p.last_signal_at),
        }
        for p in rows
    ]


@mcp.tool()
async def get_project_context(slug: str) -> dict[str, Any]:
    """Everything a session needs before working on a project: the nightly
    context pack, open deadlines, the last signals, and the next action."""
    from core import db
    from core.projects import get_project
    from core.signals import list_signals

    p = await get_project(slug)
    if p is None:
        return {"error": f"no project with slug {slug!r}"}
    row = await db.fetchrow("select context_pack, context_pack_at from projects where id = $1", p.id)
    deadlines = await db.fetch(
        "select title, due_date, priority, status from deadlines where project_id = $1 "
        "and status = 'open' order by due_date limit 10", p.id,
    )
    from core import privacy

    sigs = [privacy.model_view(s) for s in await list_signals(None, limit=10, project_id=p.id)]
    return {
        "slug": p.slug, "name": p.name, "kind": p.kind, "status": p.status, "priority": p.priority,
        "objective": p.objective, "next_action": p.next_action, "next_action_due": _iso(p.next_action_due),
        "context_pack": row["context_pack"] if row else None,
        "context_pack_at": _iso(row["context_pack_at"]) if row else None,
        "open_deadlines": [_clean(dict(d), ("title", "due_date", "priority")) for d in deadlines],
        "recent_signals": [
            {"when": _iso(s["occurred_at"]), "source": s["source"], "gist": s.get("triage_notes") or s["subject"],
             "url": s.get("url"), "urgency": s["urgency"]}
            for s in sorted(sigs, key=lambda s: s["occurred_at"], reverse=True)
        ],
    }


@mcp.tool()
async def record_outcome(
    project: str,
    summary: str,
    next_action: str | None = None,
    next_action_due: str | None = None,
    decisions: list[str] | None = None,
) -> dict[str, Any]:
    """Write back what a Claude session did on a project: an episode in
    Atlas's memory, the project's next action, and any decisions made
    (logged for the Sunday review). Use it at the end of a working session."""
    from core import db
    from core.decisions import log_decision
    from core.memory.episodic import end_episode, start_episode
    from core.projects import get_project, set_next_action

    p = await get_project(project)
    if p is None:
        return {"error": f"no project with slug {project!r}"}
    summary = (summary or "").strip()
    if not summary:
        return {"error": "summary is required"}
    episode_id = await start_episode(agent_name="claude/session", participants=["owner", "claude"])
    await db.execute("update episodes set project_id = $2 where id = $1", episode_id, p.id)
    await end_episode(episode_id, summary=summary[:4000], outcome="recorded via MCP", tags=["mcp", p.slug])
    out: dict[str, Any] = {"episode_id": str(episode_id), "project": p.slug}
    if next_action:
        due = None
        if next_action_due:
            try:
                due = date.fromisoformat(next_action_due[:10])
            except ValueError:
                return {"error": "next_action_due must be an ISO date"}
        await set_next_action(p.id, next_action.strip()[:500], due)
        out["next_action"] = next_action.strip()[:500]
    logged = []
    for d in decisions or []:
        d = (d or "").strip()
        if d:
            did = await log_decision(summary=d[:500], made_by="owner", domain=p.slug, episode_id=episode_id)
            logged.append(str(did))
    if logged:
        out["decisions"] = logged
    return out


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@mcp.tool()
async def recall_facts(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search Atlas's facts (specific things it knows about Trilok's world).
    Hybrid: meaning and exact words both count."""
    from core.memory.semantic import search_facts

    rows = await search_facts(query, limit=max(1, min(limit, 20)))
    return [{"content": r["content"], "source": r.get("source"), "confidence": r.get("confidence"),
             "matched": r.get("matched")} for r in rows]


@mcp.tool()
async def recall_lessons(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search the lessons Atlas has learned (durable takeaways that shape how it acts)."""
    from core.lessons import search_lessons

    rows = await search_lessons(query, limit=max(1, min(limit, 20)))
    return [{"content": r["content"], "domain": r.get("domain")} for r in rows]


@mcp.tool()
async def recent_lessons(limit: int = 10) -> list[dict[str, Any]]:
    """The most recent lessons, newest first."""
    from core.lessons import recent_lessons as _recent

    rows = await _recent(limit=max(1, min(limit, 50)))
    return [{"content": r["content"], "domain": r.get("domain"), "created_at": _iso(r.get("created_at"))} for r in rows]


@mcp.tool()
async def recall_episodes(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Search past work sessions (agent runs and Claude sessions written back)."""
    from core.memory.episodic import search_episodes

    rows = await search_episodes(query, limit=max(1, min(limit, 10)))
    return [{"when": _iso(r.get("ended_at") or r.get("started_at")), "agent": r["agent_name"],
             "summary": r["summary"], "outcome": r.get("outcome")} for r in rows]


@mcp.tool()
async def recent_signals(hours: int = 24, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """What arrived (GitHub, Canvas, Gmail, Calendar) in the last hours,
    most important first, optionally for one project slug."""
    from core import privacy
    from core.projects import get_project
    from core.signals import signals_for_brief

    # MCP answers go to a Claude app (a model): hidden signals as placeholders only.
    rows = [privacy.model_view(r) for r in
            await signals_for_brief(max(1, min(hours, 24 * 14)), limit=max(1, min(limit, 100)))]
    if project:
        p = await get_project(project)
        rows = [r for r in rows if p and r.get("project_id") == p.id]
    return [{"when": _iso(r["occurred_at"]), "source": r["source"], "project": r.get("project_slug"),
             "gist": r.get("triage_notes") or r["subject"], "urgency": r["urgency"], "url": r.get("url")}
            for r in rows]


@mcp.tool()
async def remember_fact(content: str, project: str | None = None) -> dict[str, Any]:
    """Store one durable fact in Atlas's memory, optionally tied to a project slug."""
    from core import db
    from core.memory.semantic import add_fact
    from core.projects import get_project

    content = (content or "").strip()
    if not content or len(content) > 4000:
        return {"error": "content must be 1 to 4000 characters"}
    fact_id = await add_fact(content=content, source="claude/mcp")
    if project:
        p = await get_project(project)
        if p:
            await db.execute("update facts set project_id = $2 where id = $1", fact_id, p.id)
    return {"fact_id": str(fact_id)}


@mcp.tool()
async def log_decision(summary: str, rationale: str | None = None, project: str | None = None,
                       review_in_days: int | None = None) -> dict[str, Any]:
    """Log a decision Trilok made so Atlas reviews it later (Right / Wrong / Mixed)."""
    from datetime import timedelta

    from core.decisions import log_decision as _log

    summary = (summary or "").strip()
    if not summary:
        return {"error": "summary is required"}
    review_at = (date.today() + timedelta(days=review_in_days)) if review_in_days else None
    did = await _log(summary=summary[:500], rationale=(rationale or "")[:2000] or None,
                     made_by="owner", domain=project, review_at=review_at)
    return {"decision_id": str(did)}


@mcp.tool()
async def add_deadline(title: str, due_date: str, project: str | None = None,
                       priority: str = "normal", remind_days_before: int | None = None) -> dict[str, Any]:
    """Add a deadline Atlas will track (and remind about). due_date is an ISO date."""
    from core import db
    from core.operations import add_deadline as _add
    from core.projects import get_project

    try:
        due = date.fromisoformat(due_date[:10])
    except (TypeError, ValueError):
        return {"error": "due_date must be an ISO date"}
    did = await _add(title=title.strip()[:200], due_date=due, domain=project, priority=priority,
                     source="claude/mcp", remind_days_before=remind_days_before)
    if project:
        p = await get_project(project)
        if p:
            await db.execute("update deadlines set project_id = $2 where id = $1", did, p.id)
    return {"deadline_id": str(did)}


@mcp.tool()
async def set_profile_note(content: str, kind: str = "fact", expires_in_days: int | None = None) -> dict[str, Any]:
    """Add a line to the profile every Atlas run sees. kind: fact, decision,
    preference, or constraint (constraints expire; default 30 days)."""
    from core.profile import set_note

    try:
        nid = await set_note(content, kind=kind, expires_in_days=expires_in_days, source="claude/mcp")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"note_id": str(nid)}


@mcp.tool()
async def rate_outcome(episode_id: str, verdict: str, note: str | None = None) -> dict[str, Any]:
    """Mark an Atlas episode good or bad (from record_outcome, recall_episodes,
    or a brief). The rating lands on every learned skill that ran in it, so
    the Sunday review can tell which adopted learnings help."""
    from core.skills import rate_episode

    try:
        affected = await rate_episode(UUID(str(episode_id)), verdict, note=note, source="claude/mcp")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"recorded": True, "skills_affected": [str(s) for s in affected]}


@mcp.tool()
async def add_night_task(title: str, definition_of_done: str, project: str | None = None,
                         agent: str | None = None) -> dict[str, Any]:
    """Put a task on tonight's night shift. Atlas works it after 23:30 inside
    the nightly cap and reports in the morning. project: a slug from
    list_projects; agent: a specialist name (defaults from the project's
    kind). The definition of done is what the agent must reach before it
    may say DONE. Added tasks start approved; the agents still never send,
    submit, post or pay."""
    from core.night import add_task

    try:
        return {"task": await add_task(title, definition_of_done, project=project, agent=agent, proposed_by="owner")}
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
async def explain_run(run_id: str) -> dict[str, Any]:
    """Why an Atlas run did what it did: what triggered it, every tool call
    with its input and result, what the ledger or an approval held it back
    from, the learned playbook in force, what it produced and remembered, and
    what it cost. run_id: a full episode id or its first 8+ characters (the
    ids in recall_episodes, record_outcome and the briefs)."""
    from core.replay import trace

    out = await trace(str(run_id), detail_chars=500)
    return out or {"error": f"no run matches {run_id!r}"}


@mcp.tool()
async def list_skills(status: str = "all", agent: str | None = None) -> list[dict[str, Any]]:
    """The learned playbook: skills adopted from reels and videos, with the
    agents they apply to and their use and rating counts. status: all,
    trial, active, or retired."""
    from core.skills import list_skills as _list

    rows = await _list(status=None if status == "all" else status, agent=agent)
    return [{"id": str(r["id"]), "title": r["title"], "body": r["body"], "status": r["status"],
             "applies_to": list(r["applies_to"]), "uses": r["uses"], "good": r["good"],
             "bad": r["bad"], "source_url": r.get("source_url")} for r in rows]


# ---------------------------------------------------------------------------
# Procedures and learnings
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_procedures(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search Atlas's procedures (step-by-step playbooks) by topic."""
    from core.memory.procedural import search_procedures as _search

    rows = await _search(query, limit=max(1, min(limit, 20)))
    return [{"path": r["path"], "name": r.get("name"), "domain": r.get("domain"),
             "description": r.get("description")} for r in rows]


@mcp.tool()
async def get_procedure(path: str) -> str:
    """The full markdown of one procedure, by its path (from search_procedures)."""
    from core.memory.procedural import load_procedure

    try:
        return await load_procedure(path)
    except FileNotFoundError:
        return f"no procedure at {path!r}"


@mcp.tool()
async def list_learnings(status: str = "proposed", limit: int = 20) -> list[dict[str, Any]]:
    """Learnings Atlas pulled from reels and videos. status: proposed,
    pursuing, drafted, adopted, rejected."""
    from core import discovery

    rows = await discovery.list_candidates(status=status, limit=max(1, min(limit, 100)))
    return [{"learning": r["learning"], "creator": r.get("creator"), "source_url": r.get("source_url"),
             "relevance": float(r["relevance"]) if r.get("relevance") is not None else None,
             "status": r.get("status")} for r in rows]


__all__ = ["mcp"]
