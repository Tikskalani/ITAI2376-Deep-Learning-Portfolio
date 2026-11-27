"""Atlas -- the PWA routes.

Everything the installed Atlas app on the iPhone needs beyond the
dashboard's existing endpoints:

  GET  /manifest.webmanifest, GET /sw.js   served at root scope
  GET  /api/push/vapid-public-key          applicationServerKey for subscribe()
  POST /api/push/subscribe                 store the PushSubscription
  DELETE /api/push/subscribe               forget it
  POST /api/push/test                      send yourself a test push
  GET  /api/approvals/pending              the open cards
  POST /api/approvals/{id}/decide          approve or reject (wakes the agent)
  GET  /api/briefs, /api/briefs/{id}       stored briefs
  GET  /api/notifications                  the inbox
  POST /api/notifications/read             mark read
  GET  /api/signals                        what arrived (triaged), by project
  GET  /api/projects                       the project table
  POST /api/decisions/{id}/review          Right / Wrong / Mixed
  POST /api/learnings/{id}/pursue          Pursue from the nightly digest
  POST /api/ingest                         iOS Shortcuts: share-to-learn,
                                           voice capture, ask; bearer token

Auth: the dashboard and these routes sit behind Cloudflare Access on the
tunnel (docs/atlas-local-host.md). /api/ingest is the one route a
Shortcut calls without the Access cookie, so it carries its own bearer
token (ATLAS_INGEST_TOKEN) and is disabled when the token is blank.
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.config import REPO_ROOT, settings
from core.decisions_review import record_review
from core.discovery import transition
from core.loop.briefs import list_briefs
from core.projects import list_projects
from core.runtime.approval import (
    check_approval,
    decide_approval,
    list_pending_approvals,
)
from core.runtime.dispatcher import dispatch
from core.signals import Signal, insert_signal, signals_for_brief
from core.skills import activate_skill, injectable, list_skills, retire_skill, review_skill
from core.surfaces import notify as notify_mod
from core.surfaces.notify import (
    add_subscription,
    get_notification,
    list_notifications,
    list_subscriptions,
    mark_read,
    push_configured,
    remove_subscription,
)

log = logging.getLogger(__name__)

router = APIRouter()
STATIC_DIR = REPO_ROOT / "core" / "web" / "static"
_URL = re.compile(r"https?://\S+")


def _jsonable(value: Any) -> Any:
    """Rows to JSON: UUIDs and datetimes to strings, recursively."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole app.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    """PushSubscription.toJSON() plus the user agent."""

    endpoint: str
    keys: SubscriptionKeys
    user_agent: str | None = None


class UnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/api/push/vapid-public-key")
async def vapid_public_key() -> dict[str, Any]:
    return {"key": settings.vapid_public_key, "configured": push_configured()}


@router.post("/api/push/subscribe")
async def push_subscribe(req: SubscribeRequest) -> dict[str, Any]:
    if not push_configured():
        raise HTTPException(status_code=503, detail="push is not configured (ATLAS_VAPID_*)")
    try:
        sid = await add_subscription(req.endpoint, req.keys.p256dh, req.keys.auth, req.user_agent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": str(sid), "count": len(await list_subscriptions())}


@router.delete("/api/push/subscribe")
async def push_unsubscribe(req: UnsubscribeRequest) -> dict[str, Any]:
    removed = await remove_subscription(req.endpoint)
    return {"removed": removed, "count": len(await list_subscriptions())}


@router.post("/api/push/test")
async def push_test() -> dict[str, Any]:
    res = await notify_mod.notify(
        kind="test",
        title="Atlas push test",
        body="If you can read this on your phone, push works.",
        priority="normal",
    )
    if res is None:
        raise HTTPException(status_code=500, detail="could not store the test notification")
    return res.model_dump(mode="json")


@router.post("/api/owes/{key}/close")
async def close_owed(key: str) -> dict[str, Any]:
    """Done on a promise in a brief: the open loop is closed for good
    (data/context.closed.yaml) and leaves every later brief."""
    from core import context_pack

    if not key.isalnum() or len(key) > 64:
        raise HTTPException(status_code=422, detail="not a loop key")
    closed = context_pack.close_loop(key)
    if closed is None:
        raise HTTPException(status_code=404, detail="no open loop with that key; it may be closed already")
    return closed


@router.get("/api/setup")
async def setup_checklist(recheck: bool = False) -> dict[str, Any]:
    """The one-time setup chores with a live status each, for Settings, Connections.
    recheck=true runs the submitter's browser check again now."""
    from core import setup_checklist as checklist_mod

    if recheck:
        from core.jobs.submit.browser import preflight

        await preflight(fresh=True)
    return _jsonable(await checklist_mod.checklist())


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class DecideRequest(BaseModel):
    decision: Literal["approved", "rejected"]


@router.get("/api/approvals/pending")
async def approvals_pending() -> dict[str, Any]:
    rows = await list_pending_approvals()
    return {"count": len(rows), "approvals": _jsonable(rows)}


@router.get("/api/approvals/{approval_id}")
async def approval_get(approval_id: UUID) -> dict[str, Any]:
    row = await check_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such approval")
    return _jsonable(row)


@router.post("/api/approvals/{approval_id}/decide")
async def approval_decide(approval_id: UUID, req: DecideRequest) -> dict[str, Any]:
    row = await decide_approval(approval_id, req.decision, decided_by="owner/pwa")
    if row is None:
        current = await check_approval(approval_id)
        if current is None:
            raise HTTPException(status_code=404, detail="no such approval")
        raise HTTPException(
            status_code=409, detail=f"already {current['status']}"
        )
    return _jsonable(row)


# ---------------------------------------------------------------------------
# Briefs, inbox, signals, projects
# ---------------------------------------------------------------------------


@router.get("/api/briefs")
async def briefs(limit: int = 10, kind: str | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit or 10), 50))
    rows = await list_briefs(limit=limit, kind=kind)
    return {"count": len(rows), "briefs": _jsonable(rows)}


@router.get("/api/briefs/{brief_id}")
async def brief_get(brief_id: UUID) -> dict[str, Any]:
    from core import db

    row = await db.fetchrow(
        "update briefs set read_at = coalesce(read_at, now()) where id = $1 returning *",
        brief_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such brief")
    return _jsonable(dict(row))


class ReadRequest(BaseModel):
    ids: list[UUID] = Field(default_factory=list)


@router.get("/api/notifications")
async def notifications(unread: bool = True, limit: int = 50, kind: str | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    rows = await list_notifications(unread_only=unread, limit=limit, kind=kind)
    return {"count": len(rows), "notifications": _jsonable(rows)}


@router.get("/api/notifications/{notification_id}")
async def notification_get(notification_id: UUID) -> dict[str, Any]:
    row = await get_notification(notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such notification")
    return _jsonable(row)


@router.post("/api/notifications/read")
async def notifications_read(req: ReadRequest) -> dict[str, Any]:
    return {"marked": await mark_read(req.ids)}


@router.get("/api/signals")
async def signals(hours: int = 24, limit: int = 40) -> dict[str, Any]:
    hours = max(1, min(int(hours or 24), 24 * 14))
    rows = await signals_for_brief(hours, limit=max(1, min(int(limit or 40), 200)))
    return {"hours": hours, "count": len(rows), "signals": _jsonable(rows)}


@router.get("/api/projects")
async def projects(status: str | None = None) -> dict[str, Any]:
    rows = await list_projects(status)
    return {"count": len(rows), "projects": [p.model_dump(mode="json") for p in rows]}


# ---------------------------------------------------------------------------
# Decision review and learnings (the taps that used to be Telegram buttons)
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    assessment: Literal["right", "wrong", "mixed", "unclear"]
    outcome: str | None = None


@router.post("/api/decisions/{decision_id}/review")
async def decision_review(decision_id: UUID, req: ReviewRequest) -> dict[str, Any]:
    try:
        recorded = await record_review(decision_id, req.assessment, req.outcome)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"recorded": bool(recorded)}


@router.post("/api/learnings/{candidate_id}/pursue")
async def learning_pursue(candidate_id: UUID) -> dict[str, Any]:
    """Pursue from the digest. The CTO drafts in the background and a
    notification says when the draft is waiting."""
    moved = await transition(candidate_id, "pursuing", from_statuses=("proposed",))
    if moved:
        from core.learning_implement import draft_in_background

        draft_in_background(candidate_id)
    return {"moved": bool(moved), "drafting": bool(moved)}


# ---------------------------------------------------------------------------
# Feedback and the learned playbook (skills)
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    verdict: Literal["good", "bad"]
    note: str | None = None


@router.post("/api/episodes/{episode_id}/feedback")
async def episode_feedback(episode_id: UUID, req: FeedbackRequest) -> dict[str, Any]:
    """Good / bad on an episode. Lands on every skill that ran in it, and a
    bad one proposes an eval case (outcome learning)."""
    from core import outcomes

    try:
        out = await outcomes.record(episode_id, req.verdict, kind="tap", note=req.note, source="owner/pwa")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"recorded": True, "skills": [str(s) for s in out["skills"]],
            "candidate_id": str(out["candidate_id"]) if out["candidate_id"] else None}


# ---------------------------------------------------------------------------
# Connect: one tap per app
# ---------------------------------------------------------------------------


class GoogleClientRequest(BaseModel):
    client_id: str = Field(min_length=10, max_length=200)
    client_secret: str = Field(min_length=5, max_length=200)


class TokenRequest(BaseModel):
    token: str = Field(min_length=5, max_length=500)


class CanvasRequest(BaseModel):
    url: str = Field(default="", max_length=200)
    token: str = Field(min_length=5, max_length=500)


class AlarmRequest(BaseModel):
    url: str = Field(min_length=12, max_length=300)


class McpRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=12, max_length=500)
    token: str | None = Field(default=None, max_length=500)
    agents: list[str] | None = None


class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


def _connect_page(title: str, body: str, *, ok: bool) -> HTMLResponse:
    """The page after Google's redirect. A failure stays long enough to read the fix path."""
    colour = "#1f9d55" if ok else "#c0392b"
    title, body = _html.escape(title), _html.escape(body)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><meta http-equiv='refresh' content='{4 if ok else 15};url=/#connect'>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;background:#0b0d10;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0}"
        "main{max-width:520px;padding:32px}h1{font-size:22px;margin:0 0 8px}a{color:#7cc4ff}</style></head>"
        f"<body><main><h1 style='color:{colour}'>{title}</h1><p>{body}</p><p><a href='/#connect'>Back to Atlas</a> (in a moment on its own)</p></main></body></html>"
    )
    return HTMLResponse(html, status_code=200)


@router.get("/api/connect")
async def connect_status(check: bool = True) -> dict[str, Any]:
    """Every source's state; check=true proves each token with its provider."""
    from core import connect

    return _jsonable(await connect.status(check=check))


@router.post("/api/connect/google/client")
async def connect_google_client(req: GoogleClientRequest) -> dict[str, Any]:
    from core import connect

    try:
        return connect.google_set_client(req.client_id, req.client_secret)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/connect/google/start")
async def connect_google_start(request: Request) -> RedirectResponse:
    """The one tap: off to Google's consent page, back to the callback."""
    from core import connect

    redirect_uri = str(request.base_url).rstrip("/") + "/api/connect/google/callback"
    try:
        return RedirectResponse(connect.google_auth_url(redirect_uri), status_code=302)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/connect/google/callback", include_in_schema=False)
async def connect_google_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    from core import connect

    if error or not code:
        why = connect.describe_consent_error(error) if error else "Google sent no sign-in code back. Try the tap again."
        return _connect_page("Google did not connect", why, ok=False)
    try:
        out = await connect.google_finish(code, state)
    except ValueError as exc:
        return _connect_page("Google did not connect", str(exc), ok=False)
    return _connect_page("Google is connected", f"{out['detail']}. Atlas reads mail and the calendar from here; drafts and holds still wait for your yes.", ok=True)


@router.post("/api/connect/google/disconnect")
async def connect_google_disconnect() -> dict[str, Any]:
    """Revoke the refresh token at Google (best effort) and forget it; the OAuth client stays."""
    from core import connect

    return await connect.google_disconnect()


@router.post("/api/connect/github")
async def connect_github(req: TokenRequest) -> dict[str, Any]:
    from core import connect

    try:
        return await connect.set_github(req.token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/connect/canvas")
async def connect_canvas(req: CanvasRequest) -> dict[str, Any]:
    from core import connect

    try:
        return await connect.set_canvas(req.url, req.token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/connect/alarm")
async def connect_alarm(req: AlarmRequest) -> dict[str, Any]:
    from core import connect

    try:
        return await connect.set_alarm(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/connect/mcp")
async def connect_mcp(req: McpRequest) -> dict[str, Any]:
    from core import connect

    try:
        return _jsonable(await connect.add_mcp_server(req.name, req.url, req.token, req.agents))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class CapRequest(BaseModel):
    cap_usd: float | None = Field(default=None, ge=0, le=500)


@router.get("/api/connect/claude")
async def connect_claude_status() -> dict[str, Any]:
    """The knowledge base from Claude: the latest import and a few of the facts."""
    from core import kb

    return _jsonable(await kb.status())


@router.post("/api/connect/claude")
async def connect_claude_upload(file: UploadFile = File(...), cap_usd: float | None = Form(default=None)) -> dict[str, Any]:  # noqa: B008
    """The claude.ai export (zip or conversations.json), queued for the worker."""
    from core import kb

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="the file is empty")
    if len(data) > 2 * 1024 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="the file is larger than 2 GB")
    try:
        return _jsonable(await kb.enqueue(data, file.filename or "", cap_usd=cap_usd))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/connect/claude/{import_id}/{action}")
async def connect_claude_control(import_id: UUID, action: Literal["pause", "resume"], req: CapRequest | None = None) -> dict[str, Any]:
    from core import kb

    row = await (kb.pause(import_id) if action == "pause" else kb.resume(import_id, cap_usd=req.cap_usd if req else None))
    if row is None:
        raise HTTPException(status_code=404, detail="no such import")
    return _jsonable(row)


@router.get("/api/connect/mcp/{name}/tools")
async def connect_mcp_tools(name: str) -> dict[str, Any]:
    """What one MCP server lets Atlas do, grouped by app, for Settings, Connections."""
    from core import connect

    try:
        return await connect.mcp_tools(name.strip().lower())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/connect/mcp/remove")
async def connect_mcp_remove(req: NameRequest) -> dict[str, Any]:
    from core import connect

    try:
        return connect.remove_mcp_server(req.name.strip().lower())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Money on Home: drop a statement, see where you stand
# ---------------------------------------------------------------------------


@router.get("/api/money")
async def money_overview(days: int = 30) -> dict[str, Any]:
    """Cash in and out over the window, the accounts, the portfolio at cost."""
    from core import money

    return _jsonable(await money.overview(days))


@router.post("/api/money/import")
async def money_import(file: UploadFile = File(...), account: str | None = Form(default=None),  # noqa: B008
                       flip_amount_sign: bool = Form(default=False)) -> dict[str, Any]:  # noqa: B008
    """A bank or card CSV, or Robinhood's transaction export, filed once."""
    from core import money

    data = await file.read()
    out = await money.import_upload(file.filename or "statement.csv", data, account=account,
                                    flip_amount_sign=flip_amount_sign)
    if "error" in out:
        raise HTTPException(status_code=422, detail=out["error"])
    return _jsonable(out)


# ---------------------------------------------------------------------------
# The trust ladder: what each tool may do on its own, and what it has earned
# ---------------------------------------------------------------------------


class GateRequest(BaseModel):
    pattern: str = Field(min_length=1, max_length=120)
    gate: Literal["auto", "approve", "deny"]
    note: str | None = Field(default=None, max_length=300)


class PatternRequest(BaseModel):
    pattern: str = Field(min_length=1, max_length=120)


@router.get("/api/trust")
async def trust_ladder(days: int = 30) -> dict[str, Any]:
    """Every rung of the approval policy with the record behind it."""
    from core import trust

    return _jsonable(await trust.ladder(days))


@router.post("/api/trust")
async def trust_set(req: GateRequest) -> dict[str, Any]:
    """His decision: move a pattern, or a bare tool name, to a rung."""
    from core import trust

    try:
        return _jsonable(await trust.set_gate(req.pattern.strip(), req.gate, (req.note or "").strip() or None))
    except trust.LockedRule as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/trust/remove")
async def trust_remove(req: PatternRequest) -> dict[str, Any]:
    from core import trust

    try:
        removed = await trust.remove(req.pattern.strip())
    except trust.LockedRule as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="no such pattern")
    return {"removed": req.pattern.strip()}


# ---------------------------------------------------------------------------
# What Atlas believes about him: visible, editable memory
# ---------------------------------------------------------------------------


class FactRequest(BaseModel):
    content: str = Field(min_length=3, max_length=2000)


class NoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=300)
    kind: Literal["fact", "decision", "constraint", "preference"] = "fact"
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


_FACT_COLS = "id, content, source, confidence, created_at, last_accessed, episode_id, project_id"


@router.get("/api/memory")
async def memory_view(q: str | None = None, limit: int = 60) -> dict[str, Any]:
    """The profile that is always in force, the people Atlas knows, and the
    facts it has kept, newest first or matching q."""
    from core import context_pack, db
    from core.memory.semantic import search_facts
    from core.profile import list_notes

    limit = max(1, min(int(limit), 200))
    q = (q or "").strip()[:200]
    facts: list[dict[str, Any]] = []
    if len(q) >= 2:
        try:
            hits = await search_facts(q, limit=limit)
            ids = [h["id"] for h in hits]
            rows = await db.fetch(f"select {_FACT_COLS} from facts where id = any($1::uuid[])", ids)
            by_id = {r["id"]: dict(r) for r in rows}
            facts = [by_id[i] for i in ids if i in by_id]
        except Exception as exc:  # noqa: BLE001 - a search that fails shows the list, not an error
            log.warning("memory view: search failed: %s", exc)
    if not facts:
        rows = await db.fetch(f"select {_FACT_COLS} from facts order by created_at desc limit $1", limit)
        facts = [dict(r) for r in rows]
    people = sorted((context_pack.load().get("people") or []), key=lambda p: (p.get("tier", 3), str(p.get("name", ""))))
    total = int(await db.fetchval("select count(*) from facts") or 0)
    return _jsonable({"query": q, "total": total, "facts": facts, "profile": await list_notes(),
                      "people": [{k: p.get(k) for k in ("name", "tier", "org", "project", "note")} for p in people]})


@router.post("/api/memory/facts")
async def memory_fact_add(req: FactRequest) -> dict[str, Any]:
    from core import db
    from core.memory.semantic import add_fact

    fid = await add_fact(req.content.strip(), source="owner")
    row = await db.fetchrow(f"select {_FACT_COLS} from facts where id = $1", fid)
    return _jsonable(dict(row))


@router.patch("/api/memory/facts/{fact_id}")
async def memory_fact_edit(fact_id: UUID, req: FactRequest) -> dict[str, Any]:
    """Correct a fact in his words. Re-embedded when the embedding model
    answers; the text is corrected either way."""
    from core import db
    from core.memory import embeddings

    content = req.content.strip()
    try:
        vec = await embeddings.embed(content)
        row = await db.fetchrow(
            f"update facts set content = $2, embedding = $3, source = 'owner/edited', confidence = 1.0 "
            f"where id = $1 returning {_FACT_COLS}",
            fact_id, content, vec,
        )
    except Exception as exc:  # noqa: BLE001 - the correction must land even when Ollama is down
        log.warning("memory view: re-embed failed, text corrected only: %s", exc)
        row = await db.fetchrow(
            f"update facts set content = $2, source = 'owner/edited', confidence = 1.0 where id = $1 returning {_FACT_COLS}",
            fact_id, content,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="no such fact")
    return _jsonable(dict(row))


@router.delete("/api/memory/facts/{fact_id}")
async def memory_fact_forget(fact_id: UUID) -> dict[str, Any]:
    from core import db

    res = await db.execute("delete from facts where id = $1", fact_id)
    if not res.endswith("1"):
        raise HTTPException(status_code=404, detail="no such fact")
    return {"forgotten": str(fact_id)}


@router.post("/api/memory/profile")
async def memory_note_add(req: NoteRequest) -> dict[str, Any]:
    from core import db
    from core.profile import set_note

    try:
        nid = await set_note(req.content, kind=req.kind, expires_in_days=req.expires_in_days, source="owner")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _jsonable(dict(await db.fetchrow("select * from profile_notes where id = $1", nid)))


@router.delete("/api/memory/profile/{note_id}")
async def memory_note_remove(note_id: UUID) -> dict[str, Any]:
    from core.profile import remove_note

    if not await remove_note(note_id):
        raise HTTPException(status_code=404, detail="no such note")
    return {"removed": str(note_id)}


@router.get("/api/outcomes")
async def outcomes_view(days: int = 7) -> dict[str, Any]:
    """The learning loop: outcomes this week against last, and the misses
    waiting to become eval cases."""
    from core import outcomes

    return _jsonable({"summary": await outcomes.summary(max(1, min(int(days), 90))),
                      "candidates": await outcomes.candidates("proposed", limit=30)})


@router.post("/api/outcomes/candidates/{candidate_id}/{action}")
async def outcomes_candidate(candidate_id: UUID, action: Literal["dismiss", "restore"]) -> dict[str, Any]:
    from core import outcomes

    row = await outcomes.set_candidate(candidate_id, "dismissed" if action == "dismiss" else "proposed")
    if row is None:
        raise HTTPException(status_code=404, detail="no such candidate")
    return _jsonable(row)


@router.get("/api/trace/{ref}")
async def run_trace(ref: str) -> dict[str, Any]:
    """Why a run did what it did: trigger, every tool call, what was held
    back, the playbook in force, what it produced, what it cost. `ref` is
    an episode id or its first 8+ characters."""
    from core.replay import trace

    if not re.fullmatch(r"[0-9a-fA-F-]{8,36}", ref or ""):
        raise HTTPException(status_code=404, detail="no run matches that id")
    out = await trace(ref)
    if out is None:
        raise HTTPException(status_code=404, detail="no run matches that id")
    return out


@router.get("/api/recordings")
async def recordings_list() -> dict[str, Any]:
    from core import recordings

    return {"recordings": await recordings.listing()}


@router.get("/api/recordings/{rec_id}")
async def recordings_get(rec_id: UUID) -> dict[str, Any]:
    from core import recordings

    row = await recordings.get(rec_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such recording")
    return {"recording": row}


@router.get("/api/recordings/{rec_id}/audio")
async def recordings_audio(rec_id: UUID) -> FileResponse:
    from core import recordings

    row = await recordings.get(rec_id)
    path = recordings.audio_path(row) if row else None
    if path is None:
        raise HTTPException(status_code=404, detail="no such recording")
    return FileResponse(path)


@router.post("/api/recordings")
async def recordings_create(
    file: UploadFile = File(...),  # noqa: B008
    kind: str = Form("lecture"),  # noqa: B008
    title: str = Form(""),  # noqa: B008
    project: str | None = Form(None),  # noqa: B008
) -> dict[str, Any]:
    """Store a recording and start transcribing in the background."""
    from core import recordings

    data = await file.read()
    try:
        row = await recordings.create(data, filename=file.filename or "recording.webm", kind=kind, title=title,
                                      project=project or None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    recordings.start_background(row["id"])
    return {"recording": row}


@router.post("/api/recordings/{rec_id}/process")
async def recordings_process(rec_id: UUID) -> dict[str, Any]:
    """Run (or rerun) the transcription and the notes for one recording."""
    from core import recordings

    if await recordings.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="no such recording")
    recordings.start_background(rec_id)
    return {"started": True}


@router.get("/api/events")
async def events_recent(limit: int = 100, since: int | None = None, kinds: str | None = None) -> dict[str, Any]:
    """What Atlas did, oldest first: an initial load, a reconnect, or a replay window."""
    from core import events

    wanted = [k for k in (kinds or "").split(",") if k] or None
    return {"events": await events.recent(limit=limit, since_id=since, kinds=wanted)}


@router.get("/api/day")
async def day_view() -> dict[str, Any]:
    """The Bridge's dial in one call: the last 24 hours done, the next 24 ahead."""
    from core import day

    return await day.day()


@router.get("/api/events/stream")
async def events_stream(since: int | None = None) -> StreamingResponse:
    """Mission control's live feed: Server-Sent Events, one JSON event per
    message, from every Atlas process. `since` backfills what a reconnect
    missed; a comment line every 15 s keeps the connection open."""
    from core import events

    return StreamingResponse(events.sse(since), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class NightTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    definition_of_done: str = Field(min_length=1, max_length=2000)
    project: str | None = None
    agent: str | None = None
    budget_usd: float | None = Field(default=None, ge=0, le=50)


class NightDecision(BaseModel):
    decision: Literal["approve", "skip"]


class NightReview(BaseModel):
    verdict: Literal["accepted", "rework"]
    note: str | None = None


@router.get("/api/night")
async def night_listing() -> dict[str, Any]:
    """Tonight's tasks (or this morning's results) and the night before."""
    from core import night

    return await night.listing()


@router.get("/api/night/replay")
async def night_replay(night: str | None = None) -> dict[str, Any]:
    """The night as a film: every event in the shift's window, in order,
    with the tasks and totals. `night` is YYYY-MM-DD (the evening's date);
    default is the most recent night."""
    from datetime import date as _date

    from core import night as night_mod

    try:
        when = _date.fromisoformat(night) if night else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="night must be YYYY-MM-DD") from exc
    return await night_mod.replay(when)


# ---------------------------------------------------------------------------
# The Planner
# ---------------------------------------------------------------------------


def _iso_date(raw: str | None, field_name: str) -> Any:
    from datetime import date as _date

    if not raw:
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD") from exc


@router.get("/api/plan")
async def plan_agenda(start: str | None = None, days: int = 7) -> dict[str, Any]:
    """The week: a bucket per day of tasks, classes, calendar events, night
    tasks and project next actions, plus what is overdue and undated."""
    from core import planner

    return _jsonable(await planner.agenda(_iso_date(start, "start"), days))


@router.get("/api/plan/parse")
async def plan_parse(text: str) -> dict[str, Any]:
    """What a quick-add line would become, for the live hint under the box."""
    from core import planner, recurring

    p = planner.parse_when(text)
    nxt = recurring.next_due(p.rule, planner.today_local() - timedelta(days=1)) if p.rule else None
    return {"title": p.title, "date": p.due_date.isoformat() if p.due_date else (nxt.isoformat() if nxt else None),
            "time": p.due_time.isoformat(timespec="minutes") if p.due_time else None,
            "priority": p.priority, "project": p.project_hint, "spans": p.spans,
            "rule": recurring.describe(p.rule) if p.rule else None}


@router.post("/api/plan/rules/{rule_id}/stop")
async def plan_rule_stop(rule_id: UUID) -> dict[str, Any]:
    """Pause a recurring rule and drop its tasks that are not due yet."""
    from core import recurring

    row = await recurring.stop_rule(rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such active rule")
    return _jsonable(row)


class QuickAddRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    project: str | None = None
    definition_of_done: str | None = Field(default=None, max_length=2000)


@router.post("/api/plan/tasks")
async def plan_quick_add(req: QuickAddRequest) -> dict[str, Any]:
    from core import planner

    try:
        task = await planner.quick_add(req.text, project=req.project, definition_of_done=req.definition_of_done)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _jsonable(task)


class RescheduleRequest(BaseModel):
    due_date: str | None = None
    time: str | None = None


@router.post("/api/plan/tasks/{task_id}/{action}")
async def plan_task_action(task_id: UUID, action: Literal["done", "reopen", "drop", "reschedule"],
                           req: RescheduleRequest | None = None) -> dict[str, Any]:
    from datetime import time as _time

    from core import planner

    if action == "reschedule":
        due_time = None
        if req and req.time:
            try:
                due_time = _time.fromisoformat(req.time)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="time must be HH:MM") from exc
        row = await planner.reschedule(task_id, _iso_date(req.due_date if req else None, "due_date"), due_time)
    else:
        row = await {"done": planner.complete, "reopen": planner.reopen, "drop": planner.drop}[action](task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such task")
    return _jsonable(row)


# ---------------------------------------------------------------------------
# The morning AI brief
# ---------------------------------------------------------------------------


@router.get("/api/news")
async def news_picks(limit: int = 12) -> dict[str, Any]:
    """The latest picks with why each matters, and the brief they went into."""
    from core import news

    return _jsonable(await news.listing(limit))


@router.post("/api/news/run")
async def news_run() -> dict[str, Any]:
    """Sweep the feeds and write this morning's brief now."""
    from core import news

    return _jsonable(await news.run_morning_brief())


@router.post("/api/night/propose")
async def night_propose_now() -> dict[str, Any]:
    from core import night

    return {"proposed": len(await night.propose(notify_when_done=False)), **(await night.listing())}


@router.post("/api/night/tasks")
async def night_add(req: NightTaskRequest) -> dict[str, Any]:
    from core import night

    try:
        return {"task": await night.add_task(req.title, req.definition_of_done, project=req.project,
                                             agent=req.agent, budget_usd=req.budget_usd)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/night/tasks/{task_id}/decide")
async def night_decide(task_id: UUID, req: NightDecision) -> dict[str, Any]:
    from core import night

    row = await night.decide(task_id, req.decision)
    if row is None:
        raise HTTPException(status_code=409, detail="that task is not open to decide")
    return {"task": row}


@router.post("/api/night/approve-all")
async def night_approve_all() -> dict[str, Any]:
    from core import night

    return {"approved": await night.approve_all()}


@router.post("/api/night/run")
async def night_run_now() -> dict[str, Any]:
    """Run the approved tasks now instead of waiting for the scheduled hour."""
    from core import night

    return await night.run_night()


@router.post("/api/night/tasks/{task_id}/review")
async def night_review(task_id: UUID, req: NightReview) -> dict[str, Any]:
    from core import night

    row = await night.review(task_id, req.verdict, note=req.note)
    if row is None:
        raise HTTPException(status_code=409, detail="only a finished task can be reviewed")
    return {"task": row}


@router.get("/api/standard")
async def standard_scorecard() -> dict[str, Any]:
    """The Atlas Standard: eight measures, each with its live number or an
    honest 'not measured yet' and what it is waiting on."""
    from core.standard import scorecard

    return await scorecard()


@router.post("/api/standard/start")
async def standard_start() -> dict[str, Any]:
    """Day one of the 14-day test is now."""
    from core.standard import scorecard, start_test

    await start_test()
    return await scorecard()


def _skill_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "slug": r["slug"], "title": r["title"], "body": r["body"],
        "domain": r.get("domain"), "applies_to": list(r.get("applies_to") or []),
        "status": r["status"], "uses": r["uses"], "good": r["good"], "bad": r["bad"],
        "source_url": r.get("source_url"), "procedure_path": r.get("procedure_path"),
        "provenance": r.get("provenance") or "unknown", "source_ref": r.get("source_ref"),
        "review": r.get("review"),
        "injected": r["status"] != "retired" and injectable(r.get("provenance"), r.get("review")),
        "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    }


@router.get("/api/skills")
async def skills_list(status: str | None = None, agent: str | None = None) -> dict[str, Any]:
    rows = await list_skills(status=status, agent=agent)
    return {"count": len(rows), "skills": [_skill_json(r) for r in rows]}


class RetireRequest(BaseModel):
    reason: str | None = None


@router.post("/api/skills/{skill_id}/retire")
async def skill_retire(skill_id: UUID, req: RetireRequest | None = None) -> dict[str, Any]:
    ok = await retire_skill(skill_id, req.reason if req else None)
    if not ok:
        raise HTTPException(status_code=404, detail="no such skill")
    return {"status": "retired"}


class SkillReviewRequest(BaseModel):
    decision: str  # approved | revoked
    note: str | None = None


@router.post("/api/skills/{skill_id}/review")
async def skill_review(skill_id: UUID, req: SkillReviewRequest) -> dict[str, Any]:
    """Trilok's approval of a skill, or its revocation. Provenance never
    changes; only an approved agent_reflection skill reaches a prompt."""
    try:
        row = await review_skill(skill_id, req.decision, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="no such skill") from exc
    return _skill_json(row)


@router.post("/api/skills/{skill_id}/keep")
async def skill_keep(skill_id: UUID) -> dict[str, Any]:
    ok = await activate_skill(skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such skill")
    return {"status": "active"}


# ---------------------------------------------------------------------------
# Workflow runs
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    owner_input: str = ""


@router.get("/api/workflows")
async def workflows_list() -> dict[str, Any]:
    from core.workflows import list_workflows

    return {"workflows": list_workflows()}


@router.get("/api/workflows/runs")
async def workflow_runs(status: str | None = None, limit: int = 20) -> dict[str, Any]:
    from core.workflow_engine import list_runs

    rows = await list_runs(status=status, limit=max(1, min(int(limit or 20), 100)))
    return {"count": len(rows), "runs": _jsonable(rows)}


@router.get("/api/workflows/runs/{run_id}")
async def workflow_run(run_id: UUID) -> dict[str, Any]:
    from core.workflow_engine import get_run, get_steps

    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return {"run": _jsonable(run), "steps": _jsonable(await get_steps(run_id))}


@router.post("/api/workflows/{name}/run")
async def workflow_start(name: str, req: RunRequest) -> dict[str, Any]:
    from core.workflow_engine import run_to_completion

    try:
        run = await run_to_completion(name, req.owner_input, trigger="pwa")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no workflow named {name!r}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _jsonable(run)


@router.post("/api/workflows/runs/{run_id}/cancel")
async def workflow_cancel(run_id: UUID) -> dict[str, Any]:
    from core.workflow_engine import cancel_run

    return {"cancelled": await cancel_run(run_id)}


# ---------------------------------------------------------------------------
# Ingest (iOS Shortcuts)
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    kind: Literal["learn", "capture", "ask"] = "learn"
    url: str | None = None
    text: str | None = None


def _check_ingest_token(authorization: str | None) -> None:
    token = settings.ingest_token
    if not token:
        raise HTTPException(status_code=503, detail="ingest is disabled (ATLAS_INGEST_TOKEN blank)")
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


@router.post("/api/ingest")
async def ingest(
    req: IngestRequest, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_ingest_token(authorization)
    text = (req.text or "").strip()
    url = (req.url or "").strip()
    if not url and text:
        m = _URL.search(text)
        if m and req.kind == "learn":
            url = m.group(0)

    if req.kind == "learn":
        if not url:
            raise HTTPException(status_code=400, detail="learn needs a url")
        if settings.capabilities:
            # The full read: transcript, frames, the linked resource, checked claims,
            # a procedure for review (core/capabilities/). Same cache policy as the
            # dashboard: a link shared again reuses its result.
            from core.web.capabilities import ShareRequest, share

            try:
                out = await share(ShareRequest(url=url))
            except HTTPException:
                raise
            except Exception as exc:
                log.exception("/api/ingest learn failed for %s", url[:120])
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return {"kind": "learn", "action": out["action"], "source_id": out["source"]["id"],
                    "status": out["source"]["status"]}
        from core.loop.discovery_scan import ingest_url

        try:
            out = await ingest_url(url)
        except Exception as exc:
            log.exception("/api/ingest learn failed for %s", url[:120])
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"kind": "learn", **_jsonable(out)}

    if not text:
        raise HTTPException(status_code=400, detail=f"{req.kind} needs text")

    if req.kind == "capture":
        now = datetime.now(UTC)
        digest = hashlib.sha256(f"{now:%Y%m%d%H%M}|{text}".encode()).hexdigest()[:16]
        sid = await insert_signal(
            Signal(
                source="capture",
                external_id=digest,
                occurred_at=now,
                actor="trilok",
                subject=text.splitlines()[0][:200],
                body=text,
                url=url or None,
                raw={"type": "capture", "via": "ingest"},
            )
        )
        return {"kind": "capture", "signal_id": str(sid) if sid else None, "duplicate": sid is None}

    try:
        result = await dispatch(
            "ceo", text, dispatched_by="surface/ingest", participants=["owner", "ceo"]
        )
    except Exception as exc:
        log.exception("/api/ingest ask failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "kind": "ask",
        "reply": (result.text or "").strip() or "(the CEO finished without a reply)",
        "episode_id": str(result.episode_id),
    }


__all__ = ["router"]
