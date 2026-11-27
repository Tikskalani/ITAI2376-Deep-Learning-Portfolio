"""Capabilities in the dashboard (core/capabilities/): share a link, watch
it process, review the procedure, approve or withdraw it, install it in
Claude Code. Every route answers 404 while ATLAS_CAPABILITIES is off.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.capabilities import claude_skill, pipeline, store
from core.config import settings

router = APIRouter()
_tasks: set[asyncio.Task[Any]] = set()


def _on() -> None:
    if not settings.capabilities:
        raise HTTPException(status_code=404, detail="capabilities are off (ATLAS_CAPABILITIES)")


def _start(source_id: str, reason: str) -> None:
    task = asyncio.create_task(pipeline.process(source_id, reason))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class ShareRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    refresh: bool = False


@router.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    _on()
    return {"sources": await store.list_sources(), "versions": await store.list_versions(),
            "cap_usd": settings.cap_source_usd}


@router.post("/api/capabilities/sources")
async def share(req: ShareRequest) -> dict[str, Any]:
    """A shared link. New: processed now. Already done or running: reused,
    nothing charged. Failed or held: left until he retries."""
    _on()
    try:
        out = await pipeline.submit(req.url, refresh=req.refresh)
    except pipeline.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if out["action"] in ("new", "retry", "refresh"):
        _start(out["source"]["id"], {"new": "first"}.get(out["action"], out["action"]))
    return out


@router.post("/api/capabilities/sources/{source_id}/retry")
async def retry(source_id: str) -> dict[str, Any]:
    """He asked: process a failed or held source again (a new attempt, charged)."""
    _on()
    src = await store.get_source(_uuid(source_id))
    if src is None:
        raise HTTPException(status_code=404, detail="no such source")
    if src["status"] == "processing":
        return {"started": False, "reason": "already running"}
    _start(src["id"], "refresh" if src["status"] == "done" else "retry")
    return {"started": True}


@router.get("/api/capabilities/versions/{version_id}")
async def version(version_id: str) -> dict[str, Any]:
    """Everything a review needs: the procedure, its evidence and its claims."""
    _on()
    v = await store.version(_uuid(version_id))
    if v is None:
        raise HTTPException(status_code=404, detail="no such version")
    attempt = UUID(v["attempt_id"]) if v.get("attempt_id") else None
    evidence = await store.evidence(attempt) if attempt else []
    for e in evidence:
        if e.get("file"):
            e["file_url"] = f"/api/capabilities/files/{e['file']}"
            e["file_present"] = (settings.capabilities_dir / e["file"]).is_file()
    return {"version": v, "evidence": evidence, "claims": await store.claims(attempt) if attempt else []}


class Review(BaseModel):
    decision: Literal["approved", "withdrawn"]
    note: str = Field(default="", max_length=1000)


@router.post("/api/capabilities/versions/{version_id}/review")
async def review(version_id: str, req: Review) -> dict[str, Any]:
    _on()
    v = await store.version(_uuid(version_id))
    if v is None:
        raise HTTPException(status_code=404, detail="no such version")
    await store.review(v["id"], req.decision, req.note or None)
    removed = await claude_skill.remove(v["id"]) if req.decision == "withdrawn" else 0
    return {"decision": req.decision, "skills_removed": removed}


@router.post("/api/capabilities/versions/{version_id}/claude")
async def install_in_claude(version_id: str) -> dict[str, Any]:
    _on()
    try:
        path = await claude_skill.install(str(_uuid(version_id)))
    except claude_skill.ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"installed": path}


@router.delete("/api/capabilities/versions/{version_id}/claude")
async def remove_from_claude(version_id: str) -> dict[str, Any]:
    _on()
    return {"removed": await claude_skill.remove(str(_uuid(version_id)))}


@router.get("/api/capabilities/files/{source_id}/{name}")
async def evidence_file(source_id: str, name: str) -> FileResponse:
    """A frame Atlas saved. Only files inside that source's own folder."""
    _on()
    base = (settings.capabilities_dir / str(_uuid(source_id))).resolve()
    path = (base / name).resolve()
    if base not in path.parents or not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(Path(path))


def _uuid(value: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
