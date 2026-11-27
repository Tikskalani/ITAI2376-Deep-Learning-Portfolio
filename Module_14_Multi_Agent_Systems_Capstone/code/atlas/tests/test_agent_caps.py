"""Minimal tests for per-agent monthly budget caps."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from core import costs


async def test_get_agent_cap_returns_none_when_unset():
    with patch("core.costs.db.fetchval", new=AsyncMock(return_value=None)):
        out = await costs.get_agent_cap("nope")
    assert out is None


async def test_enforce_agent_cap_passes_when_no_cap():
    """No row -> get_agent_cap returns None -> enforce is a no-op."""
    with patch("core.costs.get_agent_cap", new=AsyncMock(return_value=None)):
        # Should NOT raise.
        await costs.enforce_agent_cap("ceo")


async def test_enforce_agent_cap_raises_at_threshold():
    """Spend equals cap -> raise."""
    with (
        patch("core.costs.get_agent_cap",
              new=AsyncMock(return_value=Decimal("5.00"))),
        patch("core.costs.agent_month_spend",
              new=AsyncMock(return_value=Decimal("5.00"))),
    ):
        with pytest.raises(costs.CostCapExceeded):
            await costs.enforce_agent_cap("marketplace")


async def test_enforce_agent_cap_passes_under_threshold():
    with (
        patch("core.costs.get_agent_cap",
              new=AsyncMock(return_value=Decimal("5.00"))),
        patch("core.costs.agent_month_spend",
              new=AsyncMock(return_value=Decimal("2.50"))),
    ):
        await costs.enforce_agent_cap("marketplace")  # no raise
