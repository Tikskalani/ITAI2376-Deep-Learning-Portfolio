"""One-shot model calls count against the daily cap (core/llm.py, core/costs.py).

Before R2, triage and knowledge-graph extraction ran on Haiku without
creating an episode, so daily_spend never saw them. Now complete() records
each call in llm_calls with its purpose, and daily_spend sums both tables.
The Anthropic client is faked; the database is live.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core import costs, db, llm
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # daily_spend counts every call of the day; on the live database the loop
    # may record one mid-test, so the calls table is a private copy here.
    async with livedb.shadow(monkeypatch, "llm_calls"):
        yield


class _FakeMessages:
    def __init__(self, text: str, usage: SimpleNamespace) -> None:
        self.text = text
        self.usage = usage
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=self.usage,
        )


def test_usage_dict_reads_objects_and_dicts_and_drops_zeros():
    obj = SimpleNamespace(
        input_tokens=120, output_tokens=30, cache_creation_input_tokens=0,
        cache_read_input_tokens=None,
    )
    assert llm.usage_dict(obj) == {"input_tokens": 120, "output_tokens": 30}
    assert llm.usage_dict({"input_tokens": 5, "cache_read_input_tokens": 7}) == {
        "input_tokens": 5, "cache_read_input_tokens": 7,
    }
    assert llm.usage_dict(None) == {}


async def test_record_llm_call_lands_in_daily_spend():
    today = datetime.now(UTC).date()  # spend days are UTC days
    assert (await costs.daily_spend())["day"] == today.isoformat()
    before = await costs.daily_spend(today)
    cost = await costs.record_llm_call(
        "test-triage", "claude-haiku-4-5-20251001",
        {"input_tokens": 1_000_000, "output_tokens": 0}, tier="scan",
    )
    assert cost == Decimal("1.0000")  # Haiku input is $1 per million
    after = await costs.daily_spend(today)
    assert after["total"] - before["total"] == Decimal("1.0000")
    assert after["by_agent"].get("llm/test-triage") == Decimal("1.0000")
    assert after["by_model"].get("haiku", Decimal("0")) >= Decimal("1.0000")
    assert after["call_count"] == before["call_count"] + 1
    assert after["episode_count"] == before["episode_count"]


async def test_complete_records_usage_with_purpose(monkeypatch):
    from core.config import settings

    # Pinned: the price below is Haiku 4.5's; the machine's .env may name another.
    monkeypatch.setattr(settings, "model_scan", "claude-haiku-4-5-20251001")
    fake = _FakeMessages(
        "ok", SimpleNamespace(input_tokens=500, output_tokens=50,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )
    client = SimpleNamespace(messages=fake)
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: client)

    out = await llm.complete(system="s", user="u", tier="scan", purpose="test-complete")
    assert out == "ok"
    assert fake.calls[0]["model"] == llm.resolve_model("scan")
    row = await db.fetchrow(
        "select model, tier, usage, cost_usd from llm_calls "
        "where purpose = 'test-complete' order by called_at desc limit 1"
    )
    assert row is not None
    assert row["tier"] == "scan"
    assert row["usage"] == {"input_tokens": 500, "output_tokens": 50}
    # 500 in at $1/M + 50 out at $5/M = 0.0005 + 0.00025 = 0.00075 -> 0.0008
    assert Decimal(row["cost_usd"]) == Decimal("0.0008")


async def test_complete_failure_returns_empty_and_records_nothing(monkeypatch):
    class _Boom:
        async def create(self, **kw):
            raise RuntimeError("api down")

    monkeypatch.setattr(
        "core.runtime.agent.get_client", lambda: SimpleNamespace(messages=_Boom())
    )
    out = await llm.complete(system="s", user="u", purpose="test-boom")
    assert out == ""
    n = await db.fetchval("select count(*) from llm_calls where purpose = 'test-boom'")
    assert n == 0


async def test_month_spend_shape_has_call_count():
    # Pure shape check on the aggregate helper used by both spend functions.
    total, by_model, by_agent, episodes, calls = costs._aggregate([
        {"agent_name": "ceo", "tokens": {"input_tokens": 1000}, "model": None},
        {"agent_name": "llm/triage", "tokens": {"input_tokens": 1000},
         "model": "claude-haiku-4-5-20251001"},
    ])
    assert episodes == 1 and calls == 1
    assert by_agent["ceo"] == Decimal("0.0040")        # Opus input $4/M
    assert by_agent["llm/triage"] == Decimal("0.0010")  # Haiku input $1/M
    assert total == Decimal("0.0050")
    assert set(by_model) == {"opus", "haiku"}
