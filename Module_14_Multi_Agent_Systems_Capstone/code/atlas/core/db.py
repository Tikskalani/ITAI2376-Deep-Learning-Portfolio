"""Atlas Postgres connection pool.

A single asyncpg pool shared across the process. Initialized lazily on the
first call to get_pool() so that import order doesn't matter and tests
don't pay the cost when they don't need the DB.

The pool registers the pgvector codec and a JSONB codec on each new
connection so callers can pass list[float] for vector(768) columns and
plain dicts for jsonb columns without manual encoding.

Connection params are passed as kwargs rather than as a URI so that
passwords containing special characters (slashes, plus signs, etc.) don't
have to be percent-encoded by the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
from pgvector.asyncpg import register_vector

from core.config import settings

log = logging.getLogger(__name__)

# How long close_pool() waits for checked-out connections before cutting them.
POOL_CLOSE_TIMEOUT_S = 10.0

_pool: asyncpg.Pool | None = None
# The loop the pool was created on. asyncpg pools are bound to one event
# loop; using one from another loop fails with "Event loop is closed" or
# "attached to a different loop". Tracking the loop lets get_pool()
# rebuild transparently when the process moves loops (pytest, scripts
# that call asyncio.run() more than once, a server that restarts its loop).
_pool_loop: asyncio.AbstractEventLoop | None = None
# Same story for the lock: an asyncio.Lock binds to the first loop that
# uses it, so it is created lazily per loop rather than at import time.
_pool_lock: asyncio.Lock | None = None
_pool_lock_loop: asyncio.AbstractEventLoop | None = None


def _json_encode(value: object) -> str:
    """JSON encoder for the json / jsonb codecs.

    `default=str` is the safety net for the non-JSON types Atlas routinely
    stores in jsonb columns: Decimal (the CFO's amounts), date and
    datetime, UUID, and anything else with a sensible __str__. Without
    this, a tool that returns a Decimal in its result blows up the
    audit-log insert with a DataError.
    """
    return json.dumps(value, default=str)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up codecs on every new connection in the pool."""
    await register_vector(conn)
    await conn.set_type_codec(
        "jsonb",
        encoder=_json_encode,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=_json_encode,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _lock_for_current_loop() -> asyncio.Lock:
    global _pool_lock, _pool_lock_loop
    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_lock_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
    return _pool_lock


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool for the running event loop.

    Creates it on first call. If a pool exists but belongs to a different
    (usually already closed) event loop, that pool is terminated and a
    fresh one is created for the current loop.
    """
    global _pool, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is loop:
        return _pool
    async with _lock_for_current_loop():
        if _pool is not None and _pool_loop is not loop:
            # A graceful close() would need the old loop; terminate() is
            # synchronous and safe to call from here. If the old loop is
            # already closed (pytest between tests, a finished asyncio.run)
            # its transports cannot be aborted; the connections are dead
            # anyway, so dropping the pool object is the right outcome.
            # Windows' proactor loop reports that as AttributeError (its
            # socket is already None), not RuntimeError.
            old_loop_closed = _pool_loop is None or _pool_loop.is_closed()
            try:
                _pool.terminate()
            except RuntimeError as exc:
                if "closed" not in str(exc) and not old_loop_closed:
                    raise
            except AttributeError:
                if not old_loop_closed:
                    raise
            _pool = None
            _pool_loop = None
        if _pool is None:
            _pool = await asyncpg.create_pool(
                host=settings.pg_host,
                port=settings.pg_port,
                user=settings.pg_user,
                password=settings.pg_password,
                database=settings.pg_db,
                min_size=1,
                max_size=10,
                init=_init_connection,
                command_timeout=30,
            )
            _pool_loop = loop
    return _pool


async def close_pool() -> None:
    """Tear down the pool. Safe to call multiple times and from any loop."""
    global _pool, _pool_loop
    if _pool is None:
        return
    pool, pool_loop = _pool, _pool_loop
    _pool = None
    _pool_loop = None
    if pool_loop is asyncio.get_running_loop():
        # close() waits for every checked-out connection; a job stuck mid-query
        # would hold a service stop forever. Give it 10 s, then cut them.
        try:
            await asyncio.wait_for(pool.close(), timeout=POOL_CLOSE_TIMEOUT_S)
        except TimeoutError:
            log.warning("postgres pool did not close within 10 s; terminating connections")
            pool.terminate()
    else:
        pool.terminate()


async def fetch(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
