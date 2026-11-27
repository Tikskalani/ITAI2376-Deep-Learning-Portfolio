"""Atlas -- FastAPI web surface.

Runs alongside the worker (or standalone). Exposes a small REST
API over localhost so the dashboard at /static/index.html can:

  POST /api/chat        send a message to the CEO and get its reply
  GET  /api/episodes    last N episodes for the live feed
  GET  /api/cost        today / month spend + per-agent caps
  GET  /api/decisions   open decisions due for review
  GET  /api/entities    last N entities in the knowledge graph
  GET  /api/health      web, Postgres, schema and worker status (deploy check)

Security model: bind to 127.0.0.1. Remote access goes through Tailscale
Serve (reachable only from Trilok's own signed-in devices) or a Cloudflare
Tunnel behind Cloudflare Access; both put identity in front of every
request. There is no app-level login, so never expose this with Tailscale
Funnel or a tunnel without Access. scripts/deploy.ps1 refuses both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import chat_history, db
from core.config import REPO_ROOT, settings
from core.costs import (
    agent_month_spend,
    daily_spend,
    list_agent_caps,
    month_spend,
    spend_today,
)
from core.decisions_review import list_reviewable
from core.discovery import list_review_queue
from core.heartbeats import all_heartbeats
from core.loop.health import watch_forever, worker_status
from core.memory import add_fact
from core.reliability import code_revision, slo_summary
from core.runtime.dispatcher import dispatch
from core.version import VERSION

log = logging.getLogger(__name__)
# A reply as stored and shown: without the raw tool-call markup a stream can carry.
_TOOL_CALL = re.compile(r"<tool_call>[\s\S]*?</tool_call>")

STATIC_DIR = REPO_ROOT / "core" / "web" / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open the DB pool, register every agent, run the MCP session manager,
    then yield until shutdown."""
    await db.get_pool()
    # The web process needs the full agent roster registered with system
    # prompts and tools, the same way scripts/run_worker.py does. Otherwise
    # /api/chat would dispatch to a bare-named agent with no tools, and
    # /api/agents would return zero.
    try:
        from core.runtime.register_all import register_all
        register_all()
    except Exception:
        log.exception("agent registration failed in web process")
    from core.mcp_server import mcp as _mcp

    # The MCP session manager runs once per process; a second lifespan in
    # the same process (the test suite starts the app several times) just
    # reuses it.
    manager = None
    try:
        manager = _mcp.session_manager.run()
        await manager.__aenter__()
    except RuntimeError as exc:
        if "only be called once" not in str(exc):
            raise
        manager = None
    from core.service_io import startup_done

    startup_done()
    # uvicorn has its handlers by now; from here no log line carries a secret.
    from core.logredact import install as _install_redaction

    _install_redaction()
    # The loop's watchdog lives here, outside the worker it watches.
    watchdog = asyncio.create_task(watch_forever()) if settings.web_watchdog else None
    try:
        yield
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if manager is not None:
            await manager.__aexit__(None, None, None)
        await db.close_pool()


app = FastAPI(
    title="Atlas",
    version=VERSION,
    lifespan=_lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

from core.web.guard import RequestGuard  # noqa: E402

app.add_middleware(RequestGuard)

# The shell, its rooms and its scripts change together on every deploy, so
# the browser must ask before reusing a copy (a 304 when nothing changed).
# Without this a heuristic cache served yesterday's shell with today's API.
_REVALIDATE = ("/static/atlas/", "/static/rooms/")


class _Revalidate:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if path != "/" and not path.startswith(_REVALIDATE):
            await self.inner(scope, receive, send)
            return

        async def send_no_cache(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.inner(scope, receive, send_no_cache)


app.add_middleware(_Revalidate)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The PWA routes (manifest, service worker, push, approvals, briefs, inbox,
# signals, projects, ingest) live in core/web/pwa.py.
from core.web.pwa import router as _pwa_router  # noqa: E402

app.include_router(_pwa_router)

from core.web.jobs import router as _jobs_router  # noqa: E402

app.include_router(_jobs_router)

# Capabilities (K2C): shared tutorials become reviewed procedures.
from core.web.capabilities import router as _cap_router  # noqa: E402

app.include_router(_cap_router)

# The Today room (Neural Core): /api/core/*.
from core.web.core_view import router as _core_router  # noqa: E402

app.include_router(_core_router)


# The MCP server over streamable HTTP at /mcp/, behind a bearer token. A
# Claude session (Cowork, Code, mobile) adds it as a remote connector at
# https://<tunnel>/mcp/ with ATLAS_MCP_TOKEN. Blank token: 503, never open.
class _McpAuth:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        import hmac

        from starlette.responses import JSONResponse

        token = settings.mcp_token
        if not token:
            await JSONResponse({"detail": "MCP is disabled (ATLAS_MCP_TOKEN blank)"}, status_code=503)(scope, receive, send)
            return
        provided = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    provided = raw[7:].strip()
        if not provided or not hmac.compare_digest(provided, token):
            await JSONResponse({"detail": "bad or missing bearer token"}, status_code=401)(scope, receive, send)
            return
        await self.inner(scope, receive, send)


from core.mcp_server import mcp as _mcp_server  # noqa: E402

app.mount("/mcp", _McpAuth(_mcp_server.streamable_http_app()), name="mcp")


@app.get("/", response_model=None)
async def root() -> FileResponse | JSONResponse:
    """Serve the dashboard. If the static page is missing, fall back to JSON."""
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"name": "Atlas", "status": "running", "dashboard": "missing"})


# ---------------------------------------------------------------------------
# /api/chat
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    # An attached file's text rides in the message, so the cap is generous.
    message: str = Field(max_length=200_000)
    # The conversation it belongs to (core/chat_history.py); none continues the latest warm one.
    thread_id: str | None = None
    new_thread: bool = False


class ChatResponse(BaseModel):
    reply: str
    episode_id: str
    turns: int
    stop_reason: str


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Dispatch the message to the CEO. Returns the full reply."""
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is empty")
    try:
        thread = await chat_history.thread_for(req.thread_id, new=req.new_thread, first=msg)
        earlier = await chat_history.context(thread)
        await chat_history.add(thread, "you", msg)
        result = await dispatch(
            "ceo",
            msg,
            dispatched_by="surface/web",
            participants=["owner", "ceo"],
            memory_context=earlier,
        )
    except Exception as exc:
        log.exception("/api/chat dispatch failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    reply = (result.text or "").strip() or "(the CEO finished without a reply)"
    await chat_history.add(thread, "ceo", reply, result.episode_id)
    return ChatResponse(
        reply=reply,
        episode_id=str(result.episode_id),
        turns=result.turns,
        stop_reason=result.stop_reason,
    )


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Stream the CEO's reply as Server-Sent Events so the UI shows tokens
    live. Each SSE line is one run_agent_stream event as JSON."""
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is empty")

    from core.runtime.dispatcher import dispatch_stream

    thread = await chat_history.thread_for(req.thread_id, new=req.new_thread, first=msg)
    earlier = await chat_history.context(thread)
    await chat_history.add(thread, "you", msg)

    async def gen():
        # The page learns its thread first; the reply is stored when the run ends,
        # or whatever streamed so far, if the stream breaks.
        yield f"data: {json.dumps({'type': 'thread', 'id': str(thread)})}\n\n"
        text, episode = "", None
        try:
            async for event in dispatch_stream(
                "ceo", msg, participants=["owner", "ceo"], memory_context=earlier, thread_id=str(thread)
            ):
                if event.get("type") == "delta":
                    text += str(event.get("text") or "")
                elif event.get("type") == "done":
                    text = str(event.get("text") or text)
                    episode = event.get("episode_id")
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            log.exception("/api/chat/stream failed")
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
        finally:
            reply = _TOOL_CALL.sub("", text).strip() or "(the CEO finished without a reply)"
            try:
                await chat_history.add(thread, "ceo", reply, episode)
            except Exception:  # noqa: BLE001 - losing the copy must not break the reply
                log.exception("chat history: could not store the reply")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/threads")
async def chat_threads(limit: int = 30) -> dict[str, Any]:
    """The Chat channel's conversations, newest first."""
    return {"threads": await chat_history.threads(limit)}


@app.get("/api/chat/threads/{thread_id}")
async def chat_thread(thread_id: str) -> dict[str, Any]:
    found = await chat_history.messages(thread_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return found


# ---------------------------------------------------------------------------
# /api/episodes
# ---------------------------------------------------------------------------


@app.get("/api/episodes")
async def episodes(limit: int = 15) -> dict[str, Any]:
    """Most recent N episodes for the activity feed."""
    limit = max(1, min(int(limit or 15), 100))
    rows = await db.fetch(
        """
        select id, agent_name, started_at, ended_at, summary, outcome, status, result
        from episodes
        order by started_at desc
        limit $1
        """,
        limit,
    )
    return {
        "count": len(rows),
        "episodes": [
            {
                "id": str(r["id"]),
                "agent": r["agent_name"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                "summary": (r["summary"] or "")[:200],
                "outcome": (r["outcome"] or "")[:200],
                "status": r["status"],
                "result": (r["result"] or "")[:400] or None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

# The newest migration and an object it creates. The health check reports the
# schema as current only when that object exists, so a deploy that skipped
# migrations shows up as "behind". tests/test_health_endpoint.py fails until this is
# bumped alongside a new migration.
SCHEMA_PROBE = ("038_capabilities.sql", "cap_exports")
# The commit this process loaded. A route the page asks for that this
# process does not have (the Zapier tools 404 of 2026-10-20) comes from a web
# app started before a pull; health says so instead of leaving a bare 404.
BOOT_REVISION = code_revision()


@app.get("/api/health")
async def health(deep: bool = False, web: bool = False) -> JSONResponse:
    """Liveness for uptime monitors, the dashboard header and the deploy script.

    200 only when the web process, Postgres, the schema AND the background
    loop are all good; 503 otherwise, with `problems` saying what is wrong in
    plain words. A worker that just restarted counts as healthy until its
    first interval has passed (worker.state "starting").

    web=1 drops the loop from the verdict: the deploy script uses it to wait
    for the web process alone, since it restarts both services together.

    deep=1 also asks Anthropic whether the API key can use the CEO and
    specialist models (the Models API, no tokens billed). Reported under
    "anthropic", never gating the status code.
    """
    out: dict[str, Any] = {
        "ok": False,
        "status": "down",
        "version": VERSION,
        "db": "down",
        "schema": "unknown",
        "worker": {"last_tick": None, "started": None, "age_min": None, "fresh": False, "state": "unknown"},
        "push": bool(settings.vapid_public_key and settings.vapid_private_key),
        "push_devices": 0,
        "problems": [],
    }
    try:
        await db.fetchval("select 1")
        out["db"] = "ok"
        present = await db.fetchval("select to_regclass($1) is not null", f"public.{SCHEMA_PROBE[1]}")
        out["schema"] = "current" if present else "behind"
        beats = await all_heartbeats()
        out["push_devices"] = int(await db.fetchval("select count(*) from push_subscriptions") or 0)
    except Exception as exc:  # noqa: BLE001 - any failure here is "not healthy"
        log.warning("health check failed: %s", exc)
        out["problems"].append("The database is not answering.")
        return JSONResponse(out, status_code=503)
    out["worker"] = worker_status(beats)
    problems: list[str] = out["problems"]
    if out["schema"] != "current":
        problems.append("The database schema is behind; run the migrations (deploy does this).")
    state = out["worker"]["state"]
    if state == "stalled":
        age = out["worker"]["age_min"]
        problems.append(
            f"The background loop has not run for {int(age)} minutes." if age is not None
            else "The background loop started but has never completed a run."
        )
    elif state == "never":
        problems.append("The background worker has never run.")
    if out["push"] and out["push_devices"] == 0:
        out["warnings"] = ["No phone is subscribed to push, so alerts wait in the inbox. "
                           "On the phone, open Settings, Connections and follow Phone push."]
    on_disk = code_revision()
    out["code"] = {"loaded": BOOT_REVISION, "on_disk": on_disk}
    if BOOT_REVISION and on_disk and on_disk != BOOT_REVISION:
        out.setdefault("warnings", []).append(
            f"The web app is running older code ({BOOT_REVISION[:7]}) than the checkout ({on_disk[:7]}); "
            "some pages may answer 404. Restart Atlas with scripts\\deploy.bat."
        )
    try:
        out["slo"] = await slo_summary()
    except Exception as exc:  # noqa: BLE001 - the SLO report never gates health
        log.warning("slo summary failed: %s", exc)
        out["slo"] = None
    if await _google_consent_expired():
        out.setdefault("warnings", []).append(
            "Google needs a reconnect: open Connect and tap Reconnect Google. Gmail and Calendar are paused until then."
        )
    if deep:
        out["anthropic"] = await _anthropic_check()
    web_ok = out["schema"] == "current"
    loop_ok = bool(out["worker"]["fresh"])
    out["ok"] = web_ok and loop_ok
    out["status"] = "ok" if out["ok"] else ("degraded" if web_ok else "down")
    healthy = web_ok if web else out["ok"]
    return JSONResponse(out, status_code=200 if healthy else 503)


async def _google_consent_expired() -> bool:
    """The worker's Gmail or Calendar connector hit invalid_grant on its last sync."""
    try:
        n = await db.fetchval(
            "select count(*) from connector_state where source in ('gmail', 'gcal') and last_error like 'auth:%'"
        )
    except Exception:  # noqa: BLE001 - a warning is never worth failing the health check
        return False
    return bool(n)


async def _anthropic_check() -> dict[str, Any]:
    from core.llm import describe_api_error
    from core.runtime.agent import get_client

    client = get_client().with_options(timeout=15.0, max_retries=0)
    for model in dict.fromkeys([settings.model_ceo, settings.model_specialist]):
        try:
            await client.models.retrieve(model)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return {"ok": False, "detail": describe_api_error(exc, model)}
    return {"ok": True, "detail": f"key accepted; {settings.model_ceo} and {settings.model_specialist} available"}


# ---------------------------------------------------------------------------
# /api/cost
# ---------------------------------------------------------------------------


@app.get("/api/cost")
async def cost() -> dict[str, Any]:
    """Today + month spend + per-agent caps + global cap."""
    today = spend_today()
    day = await daily_spend(today)
    month = await month_spend(today.year, today.month)
    caps = await list_agent_caps()
    enriched_caps = []
    for c in caps:
        spent = await agent_month_spend(c["agent_name"])
        enriched_caps.append(
            {
                "agent": c["agent_name"],
                "monthly_cap": str(c["monthly_usd_cap"]),
                "month_spent": str(spent),
            }
        )
    return {
        "today": {
            "day": str(day["day"]),
            "total": str(day["total"]),
            "episode_count": day["episode_count"],
            "by_model": {k: str(v) for k, v in day["by_model"].items()},
            "by_agent": {k: str(v) for k, v in day["by_agent"].items()},
        },
        "month": {
            "month": month["month"],
            "total": str(month["total"]),
            "episode_count": month["episode_count"],
        },
        "global_daily_cap": float(settings.daily_usd_cap),
        "agent_caps": enriched_caps,
    }


# ---------------------------------------------------------------------------
# /api/decisions
# ---------------------------------------------------------------------------


@app.get("/api/decisions")
async def decisions_due() -> dict[str, Any]:
    """Open decisions due for review (mirrors what /review shows)."""
    rows = await list_reviewable()
    return {
        "count": len(rows),
        "decisions": [
            {
                "id": str(r["id"]),
                "summary": r["summary"],
                "rationale": r["rationale"],
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
                "review_at": r["review_at"].isoformat() if r["review_at"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/learnings -- proposed learning candidates awaiting Trilok's review
# ---------------------------------------------------------------------------


@app.get("/api/learnings")
async def learnings(limit: int = 20) -> dict[str, Any]:
    """The active learning queue (proposed + drafted) for the dashboard, with
    the CTO draft included for drafted ones."""
    rows = await list_review_queue(limit=limit)

    def _num(v: Any) -> float | None:
        return float(v) if v is not None else None

    items = [
        {
            "id": str(r["id"]),
            "learning": r["learning"],
            "creator": r["creator"],
            "title": r["title"],
            "source_url": r["source_url"],
            "relevance": _num(r["relevance"]),
            "novelty": _num(r["novelty"]),
            "credibility": _num(r["credibility"]),
            "rationale": r["rationale"],
            "status": r["status"],
            "draft": r.get("draft"),
        }
        for r in rows
    ]
    return {"count": len(items), "proposed": items}


@app.post("/api/learnings/{candidate_id}/draft")
async def learning_draft(candidate_id: UUID) -> dict[str, Any]:
    """Have the CTO draft a procedure for this learning (proposed -> drafted)."""
    from core.learning_implement import draft_candidate

    try:
        draft = await draft_candidate(candidate_id)
    except Exception as exc:
        log.exception("/api/learnings draft failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"draft": draft}


@app.post("/api/learnings/{candidate_id}/adopt")
async def learning_adopt(candidate_id: UUID) -> dict[str, Any]:
    """Adopt the draft: the skill goes into trial for the agents it names,
    the procedure (if any) is written and indexed."""
    from core.learning_implement import adopt_candidate

    try:
        out = await adopt_candidate(candidate_id)
    except Exception as exc:
        log.exception("/api/learnings adopt failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return out


@app.post("/api/learnings/{candidate_id}/reject")
async def learning_reject(candidate_id: UUID) -> dict[str, Any]:
    """Skip / discard a candidate."""
    from core.discovery import set_candidate_status

    try:
        await set_candidate_status(candidate_id, "rejected")
    except Exception as exc:
        log.exception("/api/learnings reject failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# /api/entities
# ---------------------------------------------------------------------------


@app.get("/api/entities")
async def entities(limit: int = 30) -> dict[str, Any]:
    """Most recent N entities from the knowledge graph."""
    limit = max(1, min(int(limit or 30), 200))
    rows = await db.fetch(
        """
        select id, type, name, canonical_name, metadata, created_at
        from entities
        order by created_at desc
        limit $1
        """,
        limit,
    )
    return {
        "count": len(rows),
        "entities": [
            {
                "id": str(r["id"]),
                "type": r["type"],
                "name": r["name"],
                "canonical": r["canonical_name"],
                "aliases": (r["metadata"] or {}).get("types") or [],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/cost/history -- last N days of spend for the sparkline
# ---------------------------------------------------------------------------


@app.get("/api/cost/history")
async def cost_history(days: int = 14) -> dict[str, Any]:
    """Daily spend totals for the last N days, oldest first."""
    days = max(1, min(int(days or 14), 60))
    today = spend_today()
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        spent = await daily_spend(d)
        series.append({"day": str(d), "total": str(spent["total"]),
                       "runs": spent["episode_count"]})
    return {"days": days, "series": series}


@app.get("/api/cost/panel")
async def cost_panel(days: int = 14) -> dict[str, Any]:
    """The small cost panel: cost per brief, quiet briefs, cache-write share,
    today against the cap, the month, the top spenders (core/cost_panel.py)."""
    from core.cost_panel import panel

    return await panel(days)


# ---------------------------------------------------------------------------
# /api/graph -- knowledge graph nodes + edges for the D3 viz
# ---------------------------------------------------------------------------


@app.get("/api/graph")
async def graph(limit: int = 80) -> dict[str, Any]:
    """Return knowledge-graph nodes + edges in D3-ready shape.

    Nodes are the most-recently-touched entities (capped at `limit`).
    Edges include relationships whose endpoints are both in the node set.
    """
    limit = max(10, min(int(limit or 80), 300))
    nodes_rows = await db.fetch(
        """
        select id, type, name, canonical_name, metadata,
               coalesce(updated_at, created_at) as touched_at
        from entities
        order by coalesce(updated_at, created_at) desc nulls last
        limit $1
        """,
        limit,
    )
    ids = [r["id"] for r in nodes_rows]
    if not ids:
        return {"nodes": [], "edges": []}
    edges_rows = await db.fetch(
        """
        select from_entity, to_entity, rel_type
        from relationships
        where from_entity = any($1::uuid[]) and to_entity = any($1::uuid[])
        limit 500
        """,
        ids,
    )
    return {
        "nodes": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "type": r["type"],
                "canonical": r["canonical_name"],
            }
            for r in nodes_rows
        ],
        "edges": [
            {
                "source": str(r["from_entity"]),
                "target": str(r["to_entity"]),
                "type": r["rel_type"],
            }
            for r in edges_rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/agents -- the registered roster + status per agent
# ---------------------------------------------------------------------------


@app.get("/api/memory/search")
async def memory_search(q: str, limit: int = 6) -> dict[str, Any]:
    """Facts and past runs matching q, for the command palette.

    Hybrid search on the local embedding model plus Postgres full text; no
    Claude call, so it costs nothing. When the embedding model is down the
    lexical half still answers."""
    from core.memory import search_episodes, search_facts

    q = (q or "").strip()[:200]
    if len(q) < 2:
        return {"query": q, "facts": [], "episodes": []}
    limit = max(1, min(int(limit or 6), 20))
    out: dict[str, Any] = {"query": q, "facts": [], "episodes": []}
    try:
        out["facts"] = [
            {"id": str(f["id"]), "content": (f["content"] or "")[:300], "source": f.get("source")}
            for f in await search_facts(q, limit=limit)
        ]
    except Exception as exc:  # noqa: BLE001 - one half failing should not hide the other
        log.warning("memory search (facts) failed: %s", exc)
    try:
        out["episodes"] = [
            {
                "id": str(e["id"]),
                "agent": e["agent_name"],
                "started_at": e["started_at"].isoformat() if e["started_at"] else None,
                "summary": (e["summary"] or "")[:300],
            }
            for e in await search_episodes(q, limit=limit)
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("memory search (episodes) failed: %s", exc)
    return out


@app.get("/api/agents")
async def agents() -> dict[str, Any]:
    """Return every registered agent with last-activity + monthly cap info.

    Backs the agent starfield in the dashboard.
    """
    from core.runtime.dispatcher import get_registration, registered_agents

    roster = registered_agents()
    caps = {c["agent_name"]: c for c in await list_agent_caps()}

    # Last run ever (not just this week) and runs in the last 7 days, per
    # agent, in one round-trip. Agents that ran but are no longer in the
    # roster (retired, like tutoring_support) are listed too, marked so.
    rows = await db.fetch(
        """
        select agent_name, max(started_at) as last_at,
               count(*) filter (where started_at >= now() - interval '7 days') as n
        from episodes
        where agent_name is not null
        group by agent_name
        """
    )
    by_agent = {r["agent_name"]: r for r in rows}

    out = []
    for name in list(roster) + sorted(set(by_agent) - set(roster)):
        reg = get_registration(name) if name in roster else None
        last = by_agent.get(name)
        cap = caps.get(name)
        spent = None
        if cap is not None:
            spent = str(await agent_month_spend(name))
        out.append(
            {
                "name": name,
                "tier": reg.model_tier if reg else ("retired" if name not in roster else "unknown"),
                "registered": name in roster,
                "last_active": last["last_at"].isoformat() if last and last["last_at"] else None,
                "runs_7d": int(last["n"] or 0) if last else 0,
                "monthly_cap": str(cap["monthly_usd_cap"]) if cap else None,
                "month_spent": spent,
            }
        )
    return {"count": len(out), "agents": out}


def _agent_purpose(name: str) -> str:
    """The first sentence of the agent's prompt that says what it does
    ("You are the CFO of Atlas..."), for the Staff room."""
    path = REPO_ROOT / "agents" / name / "system.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for para in text.split("\n\n"):
        para = " ".join(para.split())
        if para and not para.startswith("#"):
            return re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0][:300]
    return ""


@app.get("/api/agents/{name}")
async def agent_detail(name: str) -> dict[str, Any]:
    """One agent's page in the Staff room (audit finding 09): what it is
    for, how it has been doing, what it spent, and its recent runs, each of
    which opens in the trace view."""
    from core.runtime.dispatcher import get_registration, registered_agents

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name):
        raise HTTPException(status_code=404, detail="no such agent")
    roster = registered_agents()
    stats = await db.fetchrow(
        """
        select max(started_at) as last_at,
               count(*) filter (where started_at >= now() - interval '7 days') as runs_7d,
               count(*) filter (where started_at >= now() - interval '30 days') as runs_30d,
               count(*) filter (where started_at >= now() - interval '7 days' and status = 'failed') as failed_7d
        from episodes where agent_name = $1
        """,
        name,
    )
    if name not in roster and not (stats and stats["last_at"]):
        raise HTTPException(status_code=404, detail="no such agent")
    reg = get_registration(name) if name in roster else None
    tools: list[str] = []
    if reg and reg.tools_factory:
        try:
            tools = sorted(t.name for t in reg.tools_factory())
        except Exception:  # noqa: BLE001 - a factory that needs a live connection still leaves the page
            log.warning("agent page: tools for %s did not load", name, exc_info=True)
    cap = {c["agent_name"]: c for c in await list_agent_caps()}.get(name)
    rows = await db.fetch(
        "select id, started_at, ended_at, summary, status, result from episodes "
        "where agent_name = $1 order by started_at desc limit 20",
        name,
    )
    return {
        "name": name,
        "tier": reg.model_tier if reg else "retired",
        "registered": reg is not None,
        "purpose": _agent_purpose(name),
        "last_active": stats["last_at"].isoformat() if stats and stats["last_at"] else None,
        "runs_7d": int(stats["runs_7d"] or 0) if stats else 0,
        "runs_30d": int(stats["runs_30d"] or 0) if stats else 0,
        "failed_7d": int(stats["failed_7d"] or 0) if stats else 0,
        "month_spent": str(await agent_month_spend(name)),
        "monthly_cap": str(cap["monthly_usd_cap"]) if cap else None,
        "tools": tools,
        "episodes": [
            {
                "id": str(r["id"]),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                "summary": (r["summary"] or "")[:200],
                "status": r["status"],
                "result": (r["result"] or "")[:400] or None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/upload -- extract text from a file so the CEO can read it
# ---------------------------------------------------------------------------

# Cap upload size at 10 MB raw. Anything larger is probably the wrong file
# for a chat context anyway; recommend chunking on the user side.
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_UPLOAD_TEXT_CHAR_CAP = 60_000  # roughly 15k tokens; safe for a single turn

_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
                    ".py", ".js", ".ts", ".html", ".css", ".yml", ".yaml", ".sql"}


def _extract_pdf_text(data: bytes) -> str:
    """Lightweight PDF text extraction via pypdf. Returns extracted text."""
    import io

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
    """Accept a file, extract its text, return content + char count.

    Frontend takes the returned text and prepends it to the next chat
    message so the CEO sees it as part of the user turn. No persistent
    storage; the file lives only inside that one conversation.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="no filename")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="file is empty")
    if len(raw) > _UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({len(raw)} bytes); cap is {_UPLOAD_MAX_BYTES}",
        )

    name = file.filename
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""

    try:
        if ext == ".pdf":
            text = _extract_pdf_text(raw)
        elif ext in _TEXT_EXTENSIONS or file.content_type and file.content_type.startswith("text/"):
            text = raw.decode("utf-8", errors="replace")
        else:
            # Last-resort attempt: try UTF-8.
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=415,
                    detail=f"unsupported file type {ext!r}; try .txt, .md, .csv, .json, or .pdf",
                ) from None
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upload extraction failed for %s", name)
        raise HTTPException(status_code=500, detail=f"extraction failed: {exc}") from exc

    text = (text or "").strip()
    truncated = False
    if len(text) > _UPLOAD_TEXT_CHAR_CAP:
        text = text[:_UPLOAD_TEXT_CHAR_CAP] + "\n\n[truncated by Atlas]"
        truncated = True

    return {
        "filename": name,
        "ext": ext,
        "size": len(raw),
        "chars": len(text),
        "truncated": truncated,
        "text": text,
    }


# ---------------------------------------------------------------------------
# /api/learn -- write a fact to semantic memory (no CEO dispatch)
# ---------------------------------------------------------------------------


class LearnRequest(BaseModel):
    content: str = Field(max_length=20_000)


class LearnResponse(BaseModel):
    fact_id: str
    stored: str


@app.post("/api/learn", response_model=LearnResponse)
async def learn(req: LearnRequest) -> LearnResponse:
    """Write one fact to semantic memory. Fast path for knowledge-feeding.

    No Opus / Sonnet call, no episode created -- just an embedding +
    insert. Use this from learn-mode in the dashboard or anywhere the
    owner wants to bulk-add knowledge without spending API tokens.
    """
    text = (req.content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="content is empty")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="fact too long (max 4000 chars)")
    try:
        fact_id = await add_fact(content=text, source="owner_learn_web")
    except Exception as exc:
        log.exception("/api/learn write failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return LearnResponse(fact_id=str(fact_id), stored=text)


__all__ = ["app"]
