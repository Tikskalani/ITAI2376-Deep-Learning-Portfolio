"""Database tests that leave Trilok's real rows alone.

CI runs the suite on an empty database. On the laptop it runs on the live
one, where real notifications, signals, facts and connector cursors sit
next to the test rows. A test that scans a whole table (the unread-email
sweep, a connector merging every active project's repos, memory
consolidation) would count those rows, and could change or delete them.

shadow() gives a test private, empty copies of the tables it names. One
dedicated connection creates temp tables with the same columns, defaults
and indexes; Postgres resolves unqualified table names to the session's
temp schema first, so on that connection the code under test reads and
writes only the copies. core.db's query helpers are routed through that
connection for the duration, and closing it drops the copies.

Foreign keys are not copied, in either direction: a shadowed table can
hold ids the real tables don't have, but a real table cannot reference a
row that only exists in a copy. Shadow the tables a test scans, not
tables that real ones must point into.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest

from core import db
from core.config import settings

_TABLE = re.compile(r"[a-z_][a-z0-9_]*")
# The connection a shadow is currently routing through, if any. A shadow
# opened inside another (a test's own fixture inside the module's autouse
# one) adds its copies on that same connection: a second connection would
# silently take over the routing with none of the first one's copies.
_active: asyncpg.Connection | None = None


@asynccontextmanager
async def shadow(monkeypatch: pytest.MonkeyPatch, *tables: str) -> AsyncIterator[None]:
    """Run the body with `tables` replaced by empty private copies."""
    global _active
    for t in tables:
        if not _TABLE.fullmatch(t):
            raise ValueError(f"not a table name: {t!r}")
    if _active is not None and not _active.is_closed():
        for t in tables:
            await _active.execute(f"create temp table if not exists {t} (like public.{t} including all)")
        yield
        return
    conn = await asyncpg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        database=settings.pg_db,
        command_timeout=30,
    )
    try:
        await db._init_connection(conn)
        for t in tables:
            await conn.execute(f"create temp table {t} (like public.{t} including all)")
        # One connection serves every caller, so concurrent queries take turns.
        lock = asyncio.Lock()

        def route(method: str) -> Callable[..., Awaitable[Any]]:
            async def call(query: str, *args: Any) -> Any:
                async with lock:
                    return await getattr(conn, method)(query, *args)
            return call

        for method in ("fetch", "fetchrow", "fetchval", "execute"):
            monkeypatch.setattr(db, method, route(method))
        _active = conn
        yield
    finally:
        _active = None
        await conn.close()


# ------------------------------------------------------------------ the drift guard
#
# conftest.py hashes every live table when the session starts and
# tests/test_zz_live_tables_untouched.py (collected last) compares at the end:
# a test that wrote to a real table without shadowing it fails the suite.

# Tables the running worker and web app write on their own timers (the loop
# tick, the watchdogs, the event stream). On the laptop a change there during
# a run is reported, not failed; in CI (CI=true, nothing else runs) or with
# ATLAS_TEST_STRICT=1 every table counts.
LIVE_WRITERS = frozenset({
    "system_heartbeats", "events", "episodes", "signals", "connector_state",
    "llm_calls", "tool_ledger", "notifications", "triggers",
})


async def table_hashes() -> dict[str, str]:
    """{table: "<rows> <md5 of the sorted row md5s>"} for every public table,
    read on a connection of its own (never a shadow's)."""
    conn = await asyncpg.connect(
        host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
        password=settings.pg_password, database=settings.pg_db, command_timeout=60,
    )
    try:
        out: dict[str, str] = {}
        for r in await conn.fetch("select tablename from pg_tables where schemaname = 'public' order by 1"):
            t = r["tablename"]
            row = await conn.fetchrow(
                f'select count(*) as n, md5(coalesce(string_agg(md5(x::text), \'\' order by md5(x::text)), \'\')) as h '
                f'from public."{t}" x'
            )
            out[t] = f"{row['n']} {row['h']}"
        return out
    finally:
        await conn.close()


def snapshot() -> dict[str, str] | None:
    """table_hashes() from synchronous code; None when Postgres is not reachable."""
    try:
        return asyncio.run(table_hashes())
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return None


def drift(before: dict[str, str], after: dict[str, str], ignore: frozenset[str] = frozenset()) -> list[str]:
    """Tables whose rows changed, appeared or vanished, minus `ignore`."""
    names = sorted(set(before) | set(after))
    return [t for t in names if t not in ignore and before.get(t) != after.get(t)]
