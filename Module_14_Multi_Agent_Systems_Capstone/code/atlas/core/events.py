"""Mission control's nervous system: one stream of what Atlas is doing,
from every process, the instant it happens.

emit() writes one small row to `events` and announces it with Postgres
NOTIFY on one channel. The worker, the web app and a one-off script all
emit the same way, so the web app's subscribers see everything, not just
what its own process did. subscribe() yields events as they arrive; the
SSE route in core/web/pwa.py fans them out to the dashboard, and
recent() replays history (the morning's film is built from it).

Kinds, all past tense or present progressive, a reader can scan:
  run.started      an agent began a run            (agent, episode_id, the ask)
  run.tool         it is calling a tool            (the tool and its input, short)
  run.tool.done    the tool answered               (ok or the error)
  run.finished     the run ended                   (status, cost, turns)
  dispatch         something handed work to an agent
  approval.asked   a card went to Trilok
  approval.decided he answered it
  notified         a push or email went out
  submit.shot      the browser took a screenshot while filling a form
  submit.status    an application moved (filled, submitted, needs you)
  night.task       a night-shift task started or ended
  workflow.step    a workflow step ran

emit() never raises: the stream is a window, never a lock. A failed emit
is logged and the thing it described still happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from core import db
from core.config import settings

log = logging.getLogger(__name__)

CHANNEL = "atlas_events"
_TITLE = 200
_DETAIL = 600
_PAYLOAD = 7000  # NOTIFY payloads must stay under 8000 bytes


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, UUID)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _cut(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def emit(kind: str, *, agent: str | None = None, episode_id: UUID | str | None = None,
               title: str = "", detail: str = "", data: dict[str, Any] | None = None) -> int | None:
    """Record and announce one event. Returns its id, or None if it could not be written."""
    payload = {
        "kind": kind[:60], "agent": agent, "episode_id": str(episode_id) if episode_id else None,
        "title": _cut(title, _TITLE), "detail": _cut(detail, _DETAIL), "data": _jsonable(data or {}),
    }
    try:
        row = await db.fetchrow(
            "insert into events (kind, agent, episode_id, title, detail, data) values ($1, $2, $3, $4, $5, $6) "
            "returning id, at",
            payload["kind"], agent, UUID(str(episode_id)) if episode_id else None, payload["title"],
            payload["detail"], payload["data"],
        )
        payload["id"] = row["id"]
        payload["at"] = row["at"].isoformat()
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) > _PAYLOAD:
            payload["data"] = {"truncated": True}
            payload["detail"] = _cut(payload["detail"], 200)
            text = json.dumps(payload, ensure_ascii=False)
        await db.execute("select pg_notify($1, $2)", CHANNEL, text)
        return int(row["id"])
    except Exception:  # noqa: BLE001 - a window, never a lock
        log.exception("event %s could not be written", kind)
        return None


async def recent(*, limit: int = 100, since_id: int | None = None, kinds: list[str] | None = None,
                 start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    """Events oldest first within the window, for an initial load, a reconnect or a replay."""
    clauses: list[str] = []
    args: list[Any] = []
    if since_id is not None:
        args.append(int(since_id))
        clauses.append(f"id > ${len(args)}")
    if kinds:
        args.append(list(kinds))
        clauses.append(f"kind = any(${len(args)}::text[])")
    if start is not None:
        args.append(start)
        clauses.append(f"at >= ${len(args)}")
    if end is not None:
        args.append(end)
        clauses.append(f"at < ${len(args)}")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    args.append(max(1, min(int(limit), 2000)))
    rows = await db.fetch(
        f"select id, at, kind, agent, episode_id, title, detail, data from events {where} "
        f"order by id desc limit ${len(args)}",
        *args,
    )
    return [_jsonable(dict(r)) for r in reversed(rows)]


async def prune(days: int = 30) -> int:
    """Hygiene: the stream is not the audit log (actions and episodes are)."""
    res = await db.execute("delete from events where at < now() - make_interval(days => $1)", int(days))
    return int(res.split()[-1]) if res else 0


# ------------------------------------------------------------------ live subscribers
#
# One listener connection per process, started by the first subscriber,
# fanning every NOTIFY out to each subscriber's queue. If Postgres drops
# the connection, the listener reconnects and subscribers carry on; a
# subscriber that cannot keep up loses the oldest events, never the
# connection (the dashboard reconnects with `since` and backfills).

_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
_listener: asyncio.Task[None] | None = None
_QUEUE = 500


def _fan_out(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
    try:
        event = json.loads(payload)
    except ValueError:
        return
    for q in list(_subscribers):
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(event)


async def _listen_forever() -> None:
    delay = 1.0
    while _subscribers:
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(
                host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
                password=settings.pg_password, database=settings.pg_db,
            )
            await conn.add_listener(CHANNEL, _fan_out)
            delay = 1.0
            # asyncpg has no "closed" event to await; a once-a-second look is the honest check.
            while _subscribers and not conn.is_closed():  # noqa: ASYNC110
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconnect, do not die
            log.exception("event listener lost Postgres; reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        finally:
            if conn is not None and not conn.is_closed():
                try:
                    await conn.remove_listener(CHANNEL, _fan_out)
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass


def _ensure_listener() -> None:
    global _listener
    if _listener is None or _listener.done():
        _listener = asyncio.create_task(_listen_forever())


@asynccontextmanager
async def subscription() -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    """A queue that receives every event while the block is open. Waiting
    on it with a timeout is safe (a cancelled Queue.get loses nothing),
    which is what the SSE ping needs."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE)
    _subscribers.add(q)
    _ensure_listener()
    try:
        yield q
    finally:
        _subscribers.discard(q)


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Live events as they arrive, until the consumer stops iterating."""
    async with subscription() as q:
        while True:
            yield await q.get()


def _frame(ev: dict[str, Any]) -> str:
    return f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


async def sse(since: int | None = None, *, ping_seconds: float = 15.0) -> AsyncIterator[str]:
    """The Server-Sent Events wire format: a backfill of everything after
    `since`, a connected comment, then live events, with a comment line
    every `ping_seconds` of silence so proxies keep the connection open."""
    last = since
    if last is not None:
        for ev in await recent(limit=500, since_id=last):
            last = ev["id"]
            yield _frame(ev)
    yield ": connected\n\n"
    async with subscription() as q:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=ping_seconds)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if last is not None and int(ev.get("id") or 0) <= int(last):
                continue
            last = ev.get("id")
            yield _frame(ev)


__all__ = ["CHANNEL", "emit", "prune", "recent", "sse", "subscribe", "subscription"]
