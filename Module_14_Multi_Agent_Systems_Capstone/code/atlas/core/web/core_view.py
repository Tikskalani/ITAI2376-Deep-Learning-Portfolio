"""Routes for the Today room (Neural Core). The logic is in core/core_view.py.

  GET  /api/core/state                    agents, decisions, health, overnight, recent feed
  GET  /api/core/state?demo=1             demo: the page plays its own script; no database read
  GET  /api/core/events                   SSE: message, agent_state, decision_changed
  POST /api/core/decisions/{id}/decide    {"decision": "approve"|"decline", "idempotency_key": "..."}
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import core_view, events

router = APIRouter()

# Idempotency keys seen in the last ten minutes, with the answer they got:
# a retried hold returns the first answer instead of deciding again.
_KEY_TTL = 600.0
_KEYS: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


@router.get("/api/core/state")
async def core_state(demo: bool = False) -> dict[str, Any]:
    return core_view.demo_state() if demo else await core_view.state()


def _frame(view: dict[str, Any]) -> str:
    return f"data: {json.dumps(view, ensure_ascii=False, default=str)}\n\n"


async def stream(*, ping_seconds: float = 15.0) -> AsyncIterator[str]:
    """Live only, no backfill: the room loads its recent feed from /state, so
    a reconnect never replays old hand-offs as new packets."""
    names = {a["id"]: a["name"] for a in core_view.roster()}
    yield ": connected\n\n"
    async with events.subscription() as q:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=ping_seconds)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            for view in await core_view.to_view(ev, names):
                yield _frame(view)


@router.get("/api/core/events")
async def core_events() -> StreamingResponse:
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class CoreDecision(BaseModel):
    decision: str
    idempotency_key: str | None = None


def _remember(key: str | None, answer: dict[str, Any]) -> None:
    if not key:
        return
    _KEYS[key] = (time.monotonic(), answer)
    while len(_KEYS) > 200:
        _KEYS.popitem(last=False)


def _recall(key: str | None) -> dict[str, Any] | None:
    if not key or key not in _KEYS:
        return None
    at, answer = _KEYS[key]
    if time.monotonic() - at > _KEY_TTL:
        _KEYS.pop(key, None)
        return None
    return answer


@router.post("/api/core/decisions/{decision_id}/decide")
async def core_decide(decision_id: str, req: CoreDecision) -> dict[str, Any]:
    seen = _recall(req.idempotency_key)
    if seen is not None:
        return {**seen, "repeat": True}
    try:
        answer = await core_view.decide(decision_id, req.decision)
    except core_view.DecisionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    _remember(req.idempotency_key, answer)
    return answer
