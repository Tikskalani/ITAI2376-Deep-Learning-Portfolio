"""Atlas -- token cost accounting and the daily cap.

Atlas spends real Claude API tokens on every agent run, paid from
ANTHROPIC_API_KEY. This module turns the per-run usage dict the runtime
records on each episode into dollars, sums dollars over a day or a
month, and enforces settings.daily_usd_cap so a runaway loop cannot
drain the account overnight.

Pricing is per-million tokens, in USD, broken down by token kind:
input, output, cache writes, cache reads. Defaults reflect Anthropic's
published rates for the Opus / Sonnet / Haiku families; if a rate
changes, edit MODEL_PRICES.

The cap is enforced in run_agent (see core/runtime/agent.py). When
exceeded, CostCapExceeded is raised; the dispatcher and the Telegram
surface catch it and report a clean message instead of crashing.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from core import db
from core.config import settings

log = logging.getLogger(__name__)

# Per-million-token prices in USD, by family. Cache write is 1.25x input;
# cache read is 0.1x input. Keep the families in sync with
# settings.model_ceo / model_specialist / model_scan.
# Rates as published 2026-10 for Opus 5.5, Sonnet 5.5, Haiku 5.5 and 4.5.
# Haiku 5.5's rates hold for prompts up to 100K tokens; Atlas's scan calls
# stay far below that.
MODEL_PRICES: dict[str, dict[str, Decimal]] = {
    "opus": {
        "input": Decimal("4.00"),
        "output": Decimal("20.00"),
        "cache_write": Decimal("5.00"),
        "cache_read": Decimal("0.20"),
    },
    "sonnet": {
        "input": Decimal("2.00"),
        "output": Decimal("10.00"),
        "cache_write": Decimal("2.50"),
        "cache_read": Decimal("0.20"),
    },
    "haiku-5": {
        "input": Decimal("0.10"),
        "output": Decimal("0.50"),
        "cache_write": Decimal("0.125"),
        "cache_read": Decimal("0.01"),
    },
    "haiku": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_write": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
    },
}

# Anthropic-hosted web search is billed per request on top of tokens.
WEB_SEARCH_USD_PER_REQUEST = Decimal("0.01")

_PER_MILLION = Decimal("1000000")
_PENNY = Decimal("0.0001")  # quantize to 4dp so small calls do not round to zero


class CostCapExceeded(RuntimeError):
    """The daily token spend has hit settings.daily_usd_cap."""

    def __init__(self, spent: Decimal, cap: Decimal) -> None:
        super().__init__(
            f"daily token spend has reached the cap: "
            f"${spent} of ${cap}. Raise ATLAS_DAILY_USD_CAP or wait."
        )
        self.spent = spent
        self.cap = cap


def model_family(model: str) -> str:
    """Map a model string to its pricing family.

    Anthropic model strings include 'opus', 'sonnet', or 'haiku' in their
    name. Anything else falls back to 'sonnet' as the safe default (it is
    the middle of the three rates).
    """
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku-5" if "haiku-5" in m else "haiku"
    if "sonnet" in m:
        return "sonnet"
    return "sonnet"


def cost_of_usage(model: str, usage: dict[str, Any] | None) -> Decimal:
    """USD cost of one run's token usage. Pure math, no DB."""
    if not usage:
        return Decimal("0.0000")
    prices = MODEL_PRICES[model_family(model)]
    inp = Decimal(int(usage.get("input_tokens") or 0))
    out = Decimal(int(usage.get("output_tokens") or 0))
    cwrite = Decimal(int(usage.get("cache_creation_input_tokens") or 0))
    cread = Decimal(int(usage.get("cache_read_input_tokens") or 0))
    raw = (
        inp * prices["input"]
        + out * prices["output"]
        + cwrite * prices["cache_write"]
        + cread * prices["cache_read"]
    ) / _PER_MILLION
    searches = Decimal(int(usage.get("web_search_requests") or 0))
    raw += searches * WEB_SEARCH_USD_PER_REQUEST
    return raw.quantize(_PENNY)


# ---------------------------------------------------------------------------
# Agent name -> model resolution
# ---------------------------------------------------------------------------


def model_for_agent(agent_name: str | None) -> str:
    """Look up the concrete model used by an agent, by name.

    Tier comes from the dispatcher's in-process registry. Unknown agent
    names fall back to the specialist tier; that keeps a renamed or
    retired agent's historical rows from blowing up cost queries.
    """
    if not agent_name:
        return settings.model_specialist
    # Lazy import: avoids a circular import at module load.
    from core.runtime.dispatcher import get_registration

    reg = get_registration(agent_name)
    if reg is None:
        return settings.model_specialist
    try:
        return settings.model_for_tier(reg.model_tier)
    except ValueError:
        return settings.model_specialist


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _sum_row(row: dict[str, Any]) -> tuple[str, str, Decimal]:
    """Compute (agent, family, cost) for one spend row.

    Two row shapes arrive here. An episode row has agent_name and tokens,
    and its model is resolved from the agent's tier. An llm_calls row
    (one-shot calls: triage, graph extraction) carries its own model, and
    its agent_name is 'llm/<purpose>' so the by_agent breakdown shows
    where the non-agent spend goes.
    """
    agent = row.get("agent_name") or "unknown"
    tokens = row.get("tokens") or {}
    model = row.get("model") or model_for_agent(agent)
    return agent, model_family(model), cost_of_usage(model, tokens)


# One query returns both kinds of rows so callers (and tests that mock
# db.fetch once) see a single result set. Episode rows have model null.
_SPEND_SQL = """
    select agent_name, what_changed->'tokens' as tokens, null::text as model
    from episodes
    where {episode_where} and what_changed ? 'tokens'
    union all
    select 'llm/' || purpose as agent_name, usage as tokens, model
    from llm_calls
    where {call_where}
"""


def _aggregate(rows: list[Any]) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal], int, int]:
    total = Decimal("0")
    by_model: dict[str, Decimal] = {}
    by_agent: dict[str, Decimal] = {}
    episodes = 0
    calls = 0
    for r in rows:
        d = dict(r)
        agent, family, cost = _sum_row(d)
        total += cost
        by_model[family] = by_model.get(family, Decimal("0")) + cost
        by_agent[agent] = by_agent.get(agent, Decimal("0")) + cost
        if d.get("model"):
            calls += 1
        else:
            episodes += 1
    return total, by_model, by_agent, episodes, calls


def spend_today() -> date:
    """Today as spend is bucketed: the UTC date.

    The spend SQL casts timestamps to dates in the database's zone (UTC).
    The machine's local date differs every evening after 7pm in Houston,
    and asking for that day would read one that is already closed.
    """
    return datetime.now(UTC).date()


async def daily_spend(day: date | None = None) -> dict[str, Any]:
    """Total spend on a given calendar day (UTC). Defaults to today.

    Returns: {day, total, by_model, by_agent, episode_count, call_count}.
    Agent runs (episodes) and one-shot model calls (llm_calls) both count,
    so triage and extraction cannot run outside the cap. All money values
    are Decimal.
    """
    target = day or spend_today()
    rows = await db.fetch(
        _SPEND_SQL.format(
            episode_where="ended_at::date = $1",
            call_where="called_at::date = $1",
        ),
        target,
    )
    total, by_model, by_agent, episodes, calls = _aggregate(rows)
    return {
        "day": target.isoformat(),
        "total": total.quantize(_PENNY),
        "by_model": {k: v.quantize(_PENNY) for k, v in by_model.items()},
        "by_agent": {k: v.quantize(_PENNY) for k, v in by_agent.items()},
        "episode_count": episodes,
        "call_count": calls,
    }


async def record_llm_call(
    purpose: str,
    model: str,
    usage: dict[str, Any] | None,
    *,
    tier: str | None = None,
) -> Decimal:
    """Record one one-shot model call so it counts against the caps.

    Returns the cost. Never raises: a failed insert is logged, because a
    bookkeeping error must not fail the call that was already made.
    """
    cost = cost_of_usage(model, usage)
    try:
        await db.execute(
            """
            insert into llm_calls (purpose, model, tier, usage, cost_usd)
            values ($1, $2, $3, $4, $5)
            """,
            purpose[:100],
            model,
            tier,
            dict(usage or {}),
            cost,
        )
    except Exception:
        log.exception("could not record llm call %s on %s", purpose, model)
    return cost


async def month_spend(year: int, month: int) -> dict[str, Any]:
    """Total spend over a calendar month (UTC)."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    first = date(year, month, 1)
    rows = await db.fetch(
        _SPEND_SQL.format(
            episode_where="ended_at >= $1 and ended_at < $2",
            call_where="called_at >= $1 and called_at < $2",
        ),
        datetime(first.year, first.month, first.day),
        datetime(next_first.year, next_first.month, next_first.day),
    )
    total, by_model, _, episodes, calls = _aggregate(rows)
    return {
        "month": f"{year:04d}-{month:02d}",
        "total": total.quantize(_PENNY),
        "by_model": {k: v.quantize(_PENNY) for k, v in by_model.items()},
        "episode_count": episodes,
        "call_count": calls,
    }


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


async def enforce_daily_cap() -> None:
    """Raise CostCapExceeded if today's spend has reached the cap.

    Called at the top of every run_agent. The check is on-or-over, so
    setting the cap to $0 effectively pauses all model calls.
    """
    cap = Decimal(str(settings.daily_usd_cap))
    today = await daily_spend()
    if today["total"] >= cap:
        raise CostCapExceeded(today["total"], cap)


# ---------------------------------------------------------------------------
# Per-agent monthly budgets
# ---------------------------------------------------------------------------


async def set_agent_cap(agent_name: str, monthly_usd_cap: float) -> None:
    """Set or replace the monthly cap for one agent. Cap of 0 freezes it."""
    await db.execute(
        """
        insert into agent_caps (agent_name, monthly_usd_cap)
        values ($1, $2)
        on conflict (agent_name) do update
        set monthly_usd_cap = excluded.monthly_usd_cap,
            updated_at = now()
        """,
        agent_name,
        Decimal(str(monthly_usd_cap)),
    )


async def clear_agent_cap(agent_name: str) -> None:
    """Remove an agent's cap so it runs only against the global daily cap."""
    await db.execute(
        "delete from agent_caps where agent_name = $1", agent_name
    )


async def get_agent_cap(agent_name: str) -> Decimal | None:
    """Return the agent's monthly cap, or None if none is set."""
    val = await db.fetchval(
        "select monthly_usd_cap from agent_caps where agent_name = $1",
        agent_name,
    )
    return None if val is None else Decimal(val)


async def list_agent_caps() -> list[dict[str, Any]]:
    """All configured per-agent caps. Empty list if none set."""
    rows = await db.fetch(
        "select agent_name, monthly_usd_cap, updated_at "
        "from agent_caps order by agent_name"
    )
    return [
        {
            "agent_name": r["agent_name"],
            "monthly_usd_cap": Decimal(r["monthly_usd_cap"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def agent_month_spend(
    agent_name: str,
    year: int | None = None,
    month: int | None = None,
) -> Decimal:
    """Month-to-date spend for one agent, in dollars (Decimal)."""
    today = spend_today()
    y = year or today.year
    m = month or today.month
    if not 1 <= m <= 12:
        raise ValueError(f"month must be 1-12, got {m}")
    next_first = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    first = date(y, m, 1)
    rows = await db.fetch(
        """
        select what_changed->'tokens' as tokens
        from episodes
        where agent_name = $1
          and ended_at >= $2 and ended_at < $3
          and what_changed ? 'tokens'
        """,
        agent_name,
        datetime(first.year, first.month, first.day),
        datetime(next_first.year, next_first.month, next_first.day),
    )
    total = Decimal("0")
    for r in rows:
        _, _, cost = _sum_row({"agent_name": agent_name, "tokens": r["tokens"]})
        total += cost
    return total.quantize(_PENNY)


async def enforce_agent_cap(agent_name: str) -> None:
    """Raise CostCapExceeded if this agent's month-to-date spend hit its cap.

    No-op when no per-agent cap is configured. Called from run_agent
    right after enforce_daily_cap so both gates can fire.

    Fail-soft: if the cap lookup itself errors (DB down, table missing,
    etc.) we log and let the run proceed. The cap is a budget gate, not
    a safety gate; we should not block real work because the cap check
    crashed.
    """
    try:
        cap = await get_agent_cap(agent_name)
    except Exception:
        log.exception("agent cap lookup failed for %s; letting run proceed", agent_name)
        return
    if cap is None:
        return
    try:
        spent = await agent_month_spend(agent_name)
    except Exception:
        log.exception("agent spend lookup failed for %s; letting run proceed", agent_name)
        return
    if spent >= cap:
        raise CostCapExceeded(spent, cap)



async def daily_cost_line() -> str:
    """One-line cost summary for the evening brief.

    Example: "Today's spend: $1.23 of $8.00 cap (12 runs)." Goes into
    the brief alongside the health line so Trilok sees the running cost
    of Atlas once a day without having to ask."""
    cap = Decimal(str(settings.daily_usd_cap))
    today = await daily_spend()
    calls = today.get("call_count", 0)
    extra = f", {calls} scan calls" if calls else ""
    return (
        f"Today's spend: ${today['total']} of ${cap.quantize(Decimal('0.01'))} "
        f"cap ({today['episode_count']} runs{extra})."
    )


__all__ = [
    "MODEL_PRICES",
    "CostCapExceeded",
    "model_family",
    "model_for_agent",
    "cost_of_usage",
    "daily_spend",
    "month_spend",
    "spend_today",
    "record_llm_call",
    "enforce_daily_cap",
    "daily_cost_line",
]
