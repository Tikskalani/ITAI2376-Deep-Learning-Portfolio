"""Atlas -- the small cost panel beside the Atlas Standard scorecard (r12).

What the efficiency work is judged by, read from what Atlas already records:
the cost of each full brief (the Phase 3 stop rule is under $0.20), how many
briefs went out with no model call, the share of spend that is prompt-cache
writes, today against the cap, the month so far, and the top spenders.
Read-only; no model call.

A brief run is an episode linked to a dispatch from loop/brief/morning or
loop/brief/evening. Agent runs are priced at their tier's model, the way
core/costs.py prices them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core import db
from core.config import settings
from core.costs import cost_of_usage, daily_spend, model_for_agent, month_spend, spend_today

BRIEF_TARGET_USD = Decimal("0.20")
_CENT = Decimal("0.01")
_MILLI = Decimal("0.001")


def _cache_write_cost(model: str, usage: dict[str, Any]) -> Decimal:
    tokens = int(usage.get("cache_creation_input_tokens") or 0)
    return cost_of_usage(model, {"cache_creation_input_tokens": tokens}) if tokens else Decimal(0)


async def panel(days: int = 14) -> dict[str, Any]:
    days = max(1, min(int(days), 60))
    runs = await db.fetch(
        """
        select e.agent_name, e.started_at, e.what_changed->'tokens' as tokens,
               exists (select 1 from actions a where a.episode_id = e.id and a.action_type = 'dispatch'
                         and a.agent_name like 'loop/brief/%') as brief
          from episodes e
         where e.started_at > now() - make_interval(days => $1) and e.what_changed ? 'tokens'
         order by e.started_at
        """,
        days,
    )
    calls = await db.fetch(
        "select purpose, model, usage from llm_calls where called_at > now() - make_interval(days => $1)", days
    )
    total = writes = Decimal(0)
    by_agent: dict[str, Decimal] = {}
    brief_costs: list[Decimal] = []
    for r in runs:
        model, usage = model_for_agent(r["agent_name"]), dict(r["tokens"] or {})
        usd = cost_of_usage(model, usage)
        total += usd
        writes += _cache_write_cost(model, usage)
        by_agent[r["agent_name"]] = by_agent.get(r["agent_name"], Decimal(0)) + usd
        if r["brief"]:
            brief_costs.append(usd)
    for c in calls:
        usage = dict(c["usage"] or {})
        usd = cost_of_usage(c["model"], usage)
        total += usd
        writes += _cache_write_cost(c["model"], usage)
        key = "llm/" + (c["purpose"] or "-").split("/")[0]
        by_agent[key] = by_agent.get(key, Decimal(0)) + usd
    stored = int(await db.fetchval(
        "select count(*) from briefs where kind in ('morning', 'evening') "
        "and created_at > now() - make_interval(days => $1)", days) or 0)
    recent = brief_costs[-7:]
    today = await daily_spend(spend_today())
    now = spend_today()
    month = await month_spend(now.year, now.month)
    return {
        "days": days,
        "brief": {
            "full": len(brief_costs),
            "quiet": max(0, stored - len(brief_costs)),
            "avg_usd": str((sum(recent, Decimal(0)) / len(recent)).quantize(_MILLI)) if recent else None,
            "last_usd": str(brief_costs[-1].quantize(_MILLI)) if brief_costs else None,
            "target_usd": str(BRIEF_TARGET_USD),
        },
        "spend_usd": str(total.quantize(_CENT)),
        "cache_write_share": float((writes / total).quantize(_CENT)) if total else 0.0,
        "today_usd": str(today["total"]),
        "cap_usd": str(Decimal(str(settings.daily_usd_cap)).quantize(_CENT)),
        "month_usd": str(month["total"]),
        "top": [{"agent": a, "usd": str(v.quantize(_CENT))}
                for a, v in sorted(by_agent.items(), key=lambda kv: -kv[1])[:3]],
    }


__all__ = ["BRIEF_TARGET_USD", "panel"]
