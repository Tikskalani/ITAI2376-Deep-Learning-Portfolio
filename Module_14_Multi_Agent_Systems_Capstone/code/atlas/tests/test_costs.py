"""Unit tests for the cost layer (core/costs.py).

Pure math (cost_of_usage, model_family) is tested directly. DB-backed
functions (daily_spend, month_spend, enforce_daily_cap) are tested with
db.fetch mocked. No live Postgres, no real API calls.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from core.costs import (
    CostCapExceeded,
    cost_of_usage,
    daily_cost_line,
    daily_spend,
    enforce_daily_cap,
    model_family,
    model_for_agent,
    month_spend,
)

# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_model_family_detection():
    assert model_family("claude-opus-4-6") == "opus"
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("claude-haiku-4-5") == "haiku"
    # Unknown model strings fall back to sonnet, the safe middle rate.
    assert model_family("") == "sonnet"
    assert model_family("claude-mystery-9") == "sonnet"


def test_cost_of_usage_opus():
    # Opus 5.5: input $4/M, output $20/M, cache_read $0.20/M.
    # 1000 in, 500 out, 200 cache_read.
    # = 0.004 + 0.010 + 0.00004 = 0.01404, quantized to 0.0140
    cost = cost_of_usage(
        "claude-opus-5-5",
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200,
        },
    )
    assert cost == Decimal("0.0140")


def test_cost_of_usage_sonnet():
    # Sonnet 5.5: input $2/M, output $10/M.
    # 1000 in, 500 out = 0.002 + 0.005 = 0.0070
    cost = cost_of_usage(
        "claude-sonnet-5-5",
        {"input_tokens": 1000, "output_tokens": 500},
    )
    assert cost == Decimal("0.0070")


def test_cost_of_usage_haiku():
    # Haiku 4.5: input $1/M, output $5/M.
    # 1000 in, 500 out = 0.001 + 0.0025 = 0.0035
    cost = cost_of_usage(
        "claude-haiku-4-5-20251001",
        {"input_tokens": 1000, "output_tokens": 500},
    )
    assert cost == Decimal("0.0035")


def test_cost_of_usage_cache_write_priced_higher_than_input():
    # Cache write is 1.25x input. Sonnet input $2, cache_write $2.50.
    cost = cost_of_usage(
        "claude-sonnet-5-5",
        {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000},
    )
    assert cost == Decimal("0.0025")  # 1000 * 2.50 / 1_000_000


def test_cost_of_usage_empty_or_missing():
    assert cost_of_usage("claude-sonnet-4-6", None) == Decimal("0.0000")
    assert cost_of_usage("claude-sonnet-4-6", {}) == Decimal("0.0000")


# ---------------------------------------------------------------------------
# Agent -> model resolution
# ---------------------------------------------------------------------------


def test_model_for_known_agent_via_registry():
    # The dispatcher pre-registers every roster name. CEO uses Opus.
    assert "opus" in model_for_agent("ceo").lower()
    # Specialists use Sonnet.
    assert "sonnet" in model_for_agent("cfo").lower()
    assert "sonnet" in model_for_agent("coo").lower()


def test_model_for_unknown_agent_falls_back_to_specialist():
    # An agent name no longer in the registry shouldn't blow up.
    from core.config import settings

    assert model_for_agent("retired_agent_42") == settings.model_specialist
    assert model_for_agent(None) == settings.model_specialist


# ---------------------------------------------------------------------------
# daily_spend / month_spend
# ---------------------------------------------------------------------------


def _ep_row(agent: str, tokens: dict) -> dict:
    return {"agent_name": agent, "tokens": tokens}


async def test_daily_spend_sums_rows_by_agent_and_model():
    rows = [
        _ep_row("ceo", {"input_tokens": 10_000, "output_tokens": 2_000}),
        _ep_row("ceo", {"input_tokens": 5_000, "output_tokens": 1_000}),
        _ep_row("cfo", {"input_tokens": 8_000, "output_tokens": 1_500}),
        _ep_row("coo", {"input_tokens": 4_000, "output_tokens": 800}),
    ]
    with patch("core.costs.db.fetch", new=AsyncMock(return_value=rows)):
        out = await daily_spend(date(2026, 5, 26))

    assert out["day"] == "2026-05-26"
    assert out["episode_count"] == 4
    # CEO is on Opus, CFO+COO on Sonnet
    assert "opus" in out["by_model"]
    assert "sonnet" in out["by_model"]
    assert out["by_model"]["opus"] > Decimal("0")
    assert out["by_model"]["sonnet"] > Decimal("0")
    # CEO is most expensive (Opus + most tokens), CFO next.
    agents = sorted(out["by_agent"].items(), key=lambda kv: kv[1], reverse=True)
    assert agents[0][0] == "ceo"
    # Total is sum.
    assert out["total"] == sum(out["by_agent"].values())


async def test_daily_spend_empty():
    with patch("core.costs.db.fetch", new=AsyncMock(return_value=[])):
        out = await daily_spend(date(2026, 5, 26))
    assert out["total"] == Decimal("0.0000")
    assert out["episode_count"] == 0


async def test_month_spend_validates_month():
    with pytest.raises(ValueError):
        await month_spend(2026, 13)


async def test_month_spend_year_rollover():
    # December must compute next_first = (year+1, 1, 1) without raising.
    with patch("core.costs.db.fetch", new=AsyncMock(return_value=[])):
        out = await month_spend(2026, 12)
    assert out["month"] == "2026-12"


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


async def test_enforce_daily_cap_under_passes(monkeypatch):
    # Today's spend $1.00 against an $8 cap. Pinned here: the result must not
    # depend on the machine's .env (the cloud test .env sets the cap to 0).
    from core.config import settings

    monkeypatch.setattr(settings, "daily_usd_cap", 8.0)
    fake = {
        "day": "2026-05-26",
        "total": Decimal("1.0000"),
        "by_model": {},
        "by_agent": {},
        "episode_count": 1,
    }
    with patch("core.costs.daily_spend", new=AsyncMock(return_value=fake)):
        await enforce_daily_cap()  # no raise


async def test_enforce_daily_cap_over_raises():
    fake = {
        "day": "2026-05-26",
        "total": Decimal("8.0000"),
        "by_model": {},
        "by_agent": {},
        "episode_count": 99,
    }
    with patch("core.costs.daily_spend", new=AsyncMock(return_value=fake)):
        with pytest.raises(CostCapExceeded) as exc:
            await enforce_daily_cap()
    assert exc.value.spent == Decimal("8.0000")


async def test_cost_cap_exceeded_carries_amounts():
    err = CostCapExceeded(Decimal("9.99"), Decimal("8.00"))
    assert err.spent == Decimal("9.99")
    assert err.cap == Decimal("8.00")
    assert "9.99" in str(err) and "8.00" in str(err)


# ---------------------------------------------------------------------------
# daily_cost_line
# ---------------------------------------------------------------------------


async def test_daily_cost_line_format():
    fake = {
        "day": "2026-05-26",
        "total": Decimal("1.2345"),
        "by_model": {"sonnet": Decimal("1.2345")},
        "by_agent": {"cfo": Decimal("1.2345")},
        "episode_count": 7,
    }
    with patch("core.costs.daily_spend", new=AsyncMock(return_value=fake)):
        line = await daily_cost_line()
    assert line.startswith("Today's spend:")
    assert "$1.2345" in line
    assert "7 runs" in line


def test_cost_of_usage_haiku_5_5_and_4_5_are_priced_apart():
    usage = {"input_tokens": 100_000, "output_tokens": 10_000}
    # Haiku 5.5: $0.10/M in, $0.50/M out = 0.010 + 0.005
    assert cost_of_usage("claude-haiku-5-5", usage) == Decimal("0.0150")
    # Haiku 4.5: $1/M in, $5/M out = 0.10 + 0.05
    assert cost_of_usage("claude-haiku-4-5-20251001", usage) == Decimal("0.1500")


def test_spend_today_is_the_utc_date_not_the_local_one():
    """9pm in Houston on Oct 9 is already Oct 10 in UTC, where spend is bucketed."""
    from datetime import UTC, datetime, timedelta, timezone

    from core import costs

    houston_9pm = datetime(2026, 10, 9, 21, 0, tzinfo=timezone(timedelta(hours=-5)))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return houston_9pm.astimezone(tz or UTC)

    with patch.object(costs, "datetime", _Clock):
        assert costs.spend_today() == date(2026, 10, 10)
