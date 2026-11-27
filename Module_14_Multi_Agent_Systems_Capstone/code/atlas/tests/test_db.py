"""Unit tests for core/db.py.

The JSON encoder is exercised directly. The pool's loop-affinity logic is
exercised with a fake asyncpg.create_pool so no Postgres is needed; the
real codec wiring is covered by the live suite (tests/test_memory.py).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from core import db
from core.db import _json_encode


class _FakePool:
    def __init__(self) -> None:
        self.terminated = False
        self.closed = False

    def terminate(self) -> None:
        self.terminated = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_create_pool(monkeypatch):
    """Replace asyncpg.create_pool; record every pool handed out."""
    created: list[_FakePool] = []

    async def _create(**kwargs):
        pool = _FakePool()
        created.append(pool)
        return pool

    monkeypatch.setattr(db.asyncpg, "create_pool", _create)
    # Start from a clean module state and restore it afterwards.
    saved = (db._pool, db._pool_loop, db._pool_lock, db._pool_lock_loop)
    db._pool = db._pool_loop = db._pool_lock = db._pool_lock_loop = None
    yield created
    db._pool, db._pool_loop, db._pool_lock, db._pool_lock_loop = saved


def test_pool_is_reused_within_one_loop(fake_create_pool):
    async def body():
        a = await db.get_pool()
        b = await db.get_pool()
        assert a is b
        await db.close_pool()
        assert a.closed is True

    asyncio.run(body())
    assert len(fake_create_pool) == 1


def test_pool_is_rebuilt_when_the_loop_changes(fake_create_pool):
    """The bug this guards: a pool created on loop A was handed to loop B,
    which failed with 'Event loop is closed'. Now loop B gets a fresh pool
    and the stale one is terminated."""

    async def first():
        return await db.get_pool()

    async def second():
        return await db.get_pool()

    a = asyncio.run(first())   # loop A, then closed
    b = asyncio.run(second())  # loop B
    assert a is not b
    assert a.terminated is True
    assert b.terminated is False
    assert len(fake_create_pool) == 2


def test_stale_pool_on_a_closed_proactor_loop_is_dropped(fake_create_pool, monkeypatch):
    """On Windows, terminating a pool whose loop has closed raises
    AttributeError from the proactor transport. The connections are dead
    either way, so the new loop must still get a fresh pool."""

    def dead_transport(self) -> None:
        raise AttributeError("'NoneType' object has no attribute 'send'")

    async def first():
        return await db.get_pool()

    async def second():
        return await db.get_pool()

    a = asyncio.run(first())
    monkeypatch.setattr(_FakePool, "terminate", dead_transport)
    b = asyncio.run(second())
    assert a is not b and db._pool is b
    assert len(fake_create_pool) == 2


def test_terminate_error_on_a_live_loop_still_raises(fake_create_pool, monkeypatch):
    """Only a closed loop excuses a failed terminate; a real bug surfaces."""

    def broken(self) -> None:
        raise AttributeError("something else broke")

    async def body():
        await db.get_pool()
        other = asyncio.new_event_loop()  # a different, still-open loop
        db._pool_loop = other
        monkeypatch.setattr(_FakePool, "terminate", broken)
        try:
            with pytest.raises(AttributeError):
                await db.get_pool()
        finally:
            other.close()

    asyncio.run(body())


def test_close_pool_from_another_loop_terminates(fake_create_pool):
    async def first():
        return await db.get_pool()

    async def closer():
        await db.close_pool()

    a = asyncio.run(first())
    asyncio.run(closer())
    assert a.terminated is True
    assert a.closed is False
    assert db._pool is None


def test_json_encode_handles_plain_types():
    out = _json_encode({"a": 1, "b": "two", "c": [3, 4], "d": None})
    assert json.loads(out) == {"a": 1, "b": "two", "c": [3, 4], "d": None}


def test_json_encode_handles_decimal():
    # The CFO's cash_flow output: a nested dict with Decimal values.
    payload = {
        "income_total": Decimal("1200.00"),
        "by_stream": {"household": {"net": Decimal("-87.23")}},
    }
    out = _json_encode(payload)
    parsed = json.loads(out)
    assert parsed["income_total"] == "1200.00"
    assert parsed["by_stream"]["household"]["net"] == "-87.23"


def test_json_encode_handles_dates_and_datetimes():
    out = _json_encode(
        {"d": date(2026, 5, 1), "dt": datetime(2026, 5, 1, 14, 30)}
    )
    parsed = json.loads(out)
    assert parsed["d"] == "2026-05-01"
    assert parsed["dt"].startswith("2026-05-01")


def test_json_encode_handles_uuid():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    out = _json_encode({"id": uid})
    assert json.loads(out)["id"] == str(uid)


def test_json_encode_preserves_already_safe_payloads():
    payload = {"verdict": "no action", "count": 0, "items": []}
    assert json.loads(_json_encode(payload)) == payload
