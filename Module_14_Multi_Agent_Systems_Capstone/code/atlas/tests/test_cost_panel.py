"""The small cost panel (r12): cost per brief, quiet briefs, cache-write share. No model call."""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio

from core import db
from core.cost_panel import panel
from tests import livedb


@pytest_asyncio.fixture(autouse=True)
async def _private(monkeypatch):
    async with livedb.shadow(monkeypatch, "episodes", "actions", "briefs", "llm_calls", "notifications"):
        yield


async def _run(agent: str, tokens: dict, dispatched_by: str | None = None) -> None:
    eid = uuid4()
    await db.execute(
        "insert into episodes (id, agent_name, summary, started_at, what_changed) values ($1, $2, 'run', now(), $3)",
        eid, agent, {"tokens": tokens},
    )
    if dispatched_by:
        await db.execute(
            "insert into actions (agent_name, action_type, inputs, episode_id) values ($1, 'dispatch', $2, $3)",
            dispatched_by, {"task": "x", "target_agent": agent}, eid,
        )


async def test_panel_prices_briefs_and_counts_quiet_ones():
    # One full Opus brief: 60k cache-write tokens ($0.30) and 2k output ($0.04).
    await _run("ceo", {"cache_creation_input_tokens": 60_000, "output_tokens": 2_000}, "loop/brief/evening")
    await _run("ceo", {"input_tokens": 1_000, "output_tokens": 500})  # a chat, not a brief
    await _run("career", {"cache_creation_input_tokens": 10_000})
    for _ in range(3):  # one full brief stored, two quiet ones
        await db.execute("insert into briefs (kind, body) values ('evening', 'b')")
    out = await panel(14)
    assert out["brief"] == {"full": 1, "quiet": 2, "avg_usd": "0.340", "last_usd": "0.340", "target_usd": "0.20"}
    assert out["top"][0]["agent"] == "ceo"
    assert 0.8 < out["cache_write_share"] < 0.9


async def test_an_empty_window_reads_cleanly():
    out = await panel(7)
    assert out["brief"]["avg_usd"] is None and out["cache_write_share"] == 0.0 and out["top"] == []
