"""Unit tests for self-monitoring (core/loop/health.py) and the health
line the evening brief carries. Everything external is mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from core.loop import briefs, health
from tests import livedb


@pytest_asyncio.fixture(autouse=True)
async def _private_heartbeats(monkeypatch):
    # Briefs and the loop tick their heartbeats; a test must not make the real
    # ones look fresh, or the health check would miss a stalled loop.
    async with livedb.shadow(monkeypatch, "system_heartbeats"):
        yield


def _episode(tokens=None, tags=None):
    """An episodes row shaped like daily_health's query returns."""
    return {
        "what_changed": {"tokens": tokens or {}},
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# daily_health
# ---------------------------------------------------------------------------


async def test_daily_health_sums_tokens_across_episodes():
    rows = [
        _episode(tokens={"input_tokens": 1000, "output_tokens": 100}),
        _episode(tokens={"input_tokens": 500, "output_tokens": 50}),
    ]
    with patch("core.loop.health.db.fetch", new=AsyncMock(return_value=rows)):
        out = await health.daily_health()
    assert out["runs"] == 2
    assert out["input_tokens"] == 1500
    assert out["output_tokens"] == 150
    assert out["est_cost_usd"] >= 0


async def test_daily_health_counts_errors():
    rows = [
        _episode(tags=["agent_run", "error", "ceo"]),
        _episode(tags=["agent_run", "ceo"]),
    ]
    with patch("core.loop.health.db.fetch", new=AsyncMock(return_value=rows)):
        out = await health.daily_health()
    assert out["errors"] == 1


async def test_daily_health_handles_empty_day():
    with patch("core.loop.health.db.fetch", new=AsyncMock(return_value=[])):
        out = await health.daily_health()
    assert out == {
        "runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "errors": 0,
        "est_cost_usd": 0.0,
    }


def test_health_line_clean_day():
    line = health.health_line(
        {
            "runs": 6,
            "input_tokens": 3000,
            "output_tokens": 100,
            "cache_tokens": 0,
            "errors": 0,
            "est_cost_usd": 0.04,
        }
    )
    assert "6 agent run(s)" in line
    assert "no errors" in line
    assert "$0.04" in line


def test_health_line_flags_errors():
    line = health.health_line(
        {
            "runs": 2,
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_tokens": 0,
            "errors": 3,
            "est_cost_usd": 0.0,
        }
    )
    assert "3 error(s)" in line


# ---------------------------------------------------------------------------
# Evening brief carries the health line
# ---------------------------------------------------------------------------


async def test_evening_brief_appends_health_line():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="Evening brief body.")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)),
        patch("core.loop.briefs.notify", new=note),
        patch("core.loop.briefs._store_brief", new=AsyncMock(return_value=uuid4())),
        patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="Nothing new arrived.")),
        patch(
            "core.loop.briefs.daily_health_line",
            new=AsyncMock(return_value="System health today: all clear."),
        ),
    ):
        await briefs.run_brief("evening")
    sent = note.await_args.kwargs["body"]
    assert "Evening brief body." in sent
    assert "System health today: all clear." in sent


async def test_morning_brief_has_no_health_line():
    note = AsyncMock(return_value=None)
    result = SimpleNamespace(text="Morning brief body.")
    with (
        patch("core.loop.briefs.dispatch", new=AsyncMock(return_value=result)),
        patch("core.loop.briefs.notify", new=note),
        patch("core.loop.briefs._store_brief", new=AsyncMock(return_value=uuid4())),
        patch("core.loop.briefs.brief_block", new=AsyncMock(return_value="Nothing new arrived.")),
        patch(
            "core.loop.briefs.daily_health_line", new=AsyncMock()
        ) as health_line,
    ):
        await briefs.run_brief("morning")
    health_line.assert_not_awaited()
    assert note.await_args.kwargs["body"] == "Morning brief body."


@pytest.fixture(autouse=True)
def _no_owed_promises(monkeypatch):
    # The brief tests here compare the delivered text exactly; the promises
    # appended from config/context.yaml are covered by tests/test_owed_promises.py.
    monkeypatch.setattr("core.loop.briefs._owes", lambda: "")
