"""Atlas -- meta-eval over a window of episodes.

Evals test individual agent outputs. This module looks at the SYSTEM:
over a window of recent episodes, did the CEO route correctly? Did the
lessons store grow? Did costs stay in band? Did briefs land?

The score is a dict with named checks and a pass/fail-per-check; the
overall verdict is the unweighted average. Each check is cheap and
defined in code rather than configured in YAML, because the right
checks change as the system evolves and we want them in version
control.

Run as: python -m scripts.run_meta_eval [days]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core import db
from core.config import settings

log = logging.getLogger(__name__)


@dataclass
class MetaCheck:
    name: str
    passed: bool
    detail: str
    value: float = 0.0


@dataclass
class MetaEvalReport:
    days: int
    checks: list[MetaCheck]

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)

    def render(self) -> str:
        lines = [
            f"Meta-eval over the last {self.days} day(s)",
            f"Overall score: {self.score:.0%} "
            f"({sum(1 for c in self.checks if c.passed)}/{len(self.checks)} checks pass)",
            "",
        ]
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"[{mark}] {c.name}: {c.detail}")
        return "\n".join(lines)


async def _check_episodes_landed(days: int) -> MetaCheck:
    """At least one episode per day on average."""
    n = await db.fetchval(
        "select count(*) from episodes "
        "where started_at >= now() - ($1 * interval '1 day')",
        days,
    )
    n = int(n or 0)
    passed = n >= days  # one episode per day minimum
    return MetaCheck(
        name="episodes_landed",
        passed=passed,
        detail=f"{n} episode(s) in {days} day(s)",
        value=float(n),
    )


async def _check_ceo_synthesis(days: int) -> MetaCheck:
    """CEO produced at least 5 successful syntheses in the window."""
    n = await db.fetchval(
        """
        select count(*) from episodes
        where agent_name = 'ceo'
          and started_at >= now() - ($1 * interval '1 day')
          and status = 'ok'
        """,
        days,
    )
    n = int(n or 0)
    passed = n >= 5
    return MetaCheck(
        name="ceo_synthesis",
        passed=passed,
        detail=f"{n} CEO synthesis episode(s)",
        value=float(n),
    )


async def _check_lessons_growing(days: int) -> MetaCheck:
    """The lessons store grew by at least 3 rows in the window."""
    n = await db.fetchval(
        "select count(*) from lessons "
        "where created_at >= now() - ($1 * interval '1 day')",
        days,
    )
    n = int(n or 0)
    passed = n >= 3
    return MetaCheck(
        name="lessons_growing",
        passed=passed,
        detail=f"{n} new lesson(s)",
        value=float(n),
    )


async def _check_decisions_logged(days: int) -> MetaCheck:
    """At least one decision was logged in the window."""
    n = await db.fetchval(
        "select count(*) from decisions "
        "where decided_at >= now() - ($1 * interval '1 day')",
        days,
    )
    n = int(n or 0)
    passed = n >= 1
    return MetaCheck(
        name="decisions_logged",
        passed=passed,
        detail=f"{n} decision(s) recorded",
        value=float(n),
    )


async def _check_entities_growing(days: int) -> MetaCheck:
    """The knowledge graph picked up at least 3 new entities."""
    n = await db.fetchval(
        "select count(*) from entities "
        "where created_at >= now() - ($1 * interval '1 day')",
        days,
    )
    n = int(n or 0)
    passed = n >= 3
    return MetaCheck(
        name="entities_growing",
        passed=passed,
        detail=f"{n} new entity row(s)",
        value=float(n),
    )


async def _check_cost_in_band(days: int) -> MetaCheck:
    """Average daily spend stays under the configured cap."""
    # Cost comes from the daily_spend helper so pricing logic lives in one place.
    from core.costs import daily_spend, spend_today

    today = spend_today()
    totals = []
    for i in range(days):
        d = today - timedelta(days=i)
        spent = (await daily_spend(d))["total"]
        totals.append(float(spent))
    avg = sum(totals) / max(1, len(totals))
    cap = settings.daily_usd_cap
    passed = avg <= cap
    return MetaCheck(
        name="cost_in_band",
        passed=passed,
        detail=f"avg ${avg:.2f}/day vs ${cap:.2f} cap",
        value=avg,
    )


async def _check_no_loop_stalls(days: int) -> MetaCheck:
    """The executive loop heartbeat was set in the last hour."""
    last = await db.fetchval(
        "select last_seen_at from system_heartbeats "
        "where kind = 'loop/executive'"
    )
    if not last:
        return MetaCheck(
            name="loop_alive",
            passed=False,
            detail="no executive-loop heartbeat ever recorded",
        )
    age = datetime.now(UTC) - last
    passed = age <= timedelta(hours=1)
    return MetaCheck(
        name="loop_alive",
        passed=passed,
        detail=f"executive loop last seen {int(age.total_seconds() / 60)}m ago",
        value=age.total_seconds(),
    )


async def _check_playbook_in_use(days: int) -> MetaCheck:
    """Every trial or active skill older than a day should have run at
    least once in the window; a skill nobody runs is not teaching anyone."""
    row = await db.fetchrow(
        """
        select count(*) as total,
               count(*) filter (where last_used_at >= now() - make_interval(days => $1)) as used
        from skills
        where status in ('trial', 'active') and created_at < now() - interval '1 day'
        """,
        days,
    )
    total, used = int(row["total"]), int(row["used"])
    if total == 0:
        return MetaCheck(name="playbook_in_use", passed=True,
                         detail="no adopted skills yet", value=0.0)
    return MetaCheck(name="playbook_in_use", passed=used == total,
                     detail=f"{used}/{total} adopted skills ran in the window",
                     value=used / total)


async def run_meta_eval(days: int = 7) -> MetaEvalReport:
    """Run all meta-checks over the last `days` days. Never raises."""
    days = max(1, min(int(days or 7), 30))
    checks: list[MetaCheck] = []
    for fn in [
        _check_episodes_landed,
        _check_ceo_synthesis,
        _check_lessons_growing,
        _check_decisions_logged,
        _check_entities_growing,
        _check_cost_in_band,
        _check_no_loop_stalls,
        _check_playbook_in_use,
    ]:
        try:
            checks.append(await fn(days))
        except Exception as exc:
            log.exception("meta-eval check %s failed", fn.__name__)
            checks.append(
                MetaCheck(
                    name=fn.__name__.removeprefix("_check_"),
                    passed=False,
                    detail=f"check crashed: {type(exc).__name__}: {exc}",
                )
            )
    return MetaEvalReport(days=days, checks=checks)


__all__ = ["MetaCheck", "MetaEvalReport", "run_meta_eval"]
