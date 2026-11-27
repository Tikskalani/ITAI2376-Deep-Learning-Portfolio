"""close_pool never hangs a service stop on a stuck connection."""

from __future__ import annotations

import asyncio

from core import db


class _StuckPool:
    def __init__(self) -> None:
        self.terminated = False

    async def close(self) -> None:
        await asyncio.sleep(3600)

    def terminate(self) -> None:
        self.terminated = True


async def test_close_pool_terminates_when_close_hangs(monkeypatch):
    pool = _StuckPool()
    monkeypatch.setattr(db, "_pool", pool)
    monkeypatch.setattr(db, "_pool_loop", asyncio.get_running_loop())
    monkeypatch.setattr(db, "POOL_CLOSE_TIMEOUT_S", 0.05)
    await asyncio.wait_for(db.close_pool(), timeout=2)
    assert pool.terminated is True
    assert db._pool is None
