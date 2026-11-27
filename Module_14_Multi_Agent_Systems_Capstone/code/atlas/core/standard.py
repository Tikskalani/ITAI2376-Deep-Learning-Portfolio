"""The Atlas Standard: the eight measures from docs/atlas-vision-2026-10-10.md
section 8, computed from the tables Atlas already writes.

The standard is a dashboard Trilok watches move, not a claim. So this is
honest by construction: a measure the data can back today shows its live
number and whether it clears the bar; one it cannot back yet says so and
names what it is waiting on. Nothing is estimated.

The 14-day test starts when he taps "Start": that stamps a heartbeat and
the card counts the days from there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core import db
from core.config import REPO_ROOT
from core.costs import daily_spend
from core.heartbeats import set_heartbeat
from core.runtime.policy import list_policies, match_gate

TEST_DAYS = 14
STARTED_KIND = "standard/started"
EVAL_DATA = REPO_ROOT / "evals" / "data"
# An application that reached an employer. "interested" is a draft.
SUBMITTED = ("applied", "screening", "interview", "offer", "rejected", "ghosted")


def _row(key: str, name: str, bar: str, *, value: str, status: str, detail: str) -> dict[str, Any]:
    """status: pass, fail, or none (not measured yet)."""
    return {"key": key, "name": name, "bar": bar, "value": value, "status": status, "detail": detail}


async def start_test() -> datetime:
    """Day one of the 14-day test is now."""
    await set_heartbeat(STARTED_KIND, {"days": TEST_DAYS})
    return datetime.now(UTC)


async def _trust() -> dict[str, Any]:
    policies = await list_policies()
    tools = [r["tool"] for r in await db.fetch(
        "select distinct inputs->>'tool' as tool from actions where action_type = 'tool_call' and inputs ? 'tool'"
    )]
    gated = [t for t in tools if t and match_gate(t, policies)[0] == "approve"]
    row = await db.fetchrow(
        """
        select count(*) filter (where approved_by = 'auto') as unapproved,
               count(*) filter (where approved_by <> 'auto') as gated_calls
          from actions where action_type = 'tool_call' and inputs->>'tool' = any($1::text[])
        """,
        gated,
    )
    approvals = await db.fetchrow(
        "select count(*) as asked, count(*) filter (where status = 'approved') as approved, "
        "count(*) filter (where status = 'rejected') as rejected from approvals"
    )
    repeats = int(await db.fetchval("select count(*) from tool_ledger where attempts > 1") or 0)
    caught = int(await db.fetchval("select count(*) from job_events where kind = 'blocked'") or 0)
    return {
        "unapproved": int(row["unapproved"] or 0), "gated_calls": int(row["gated_calls"] or 0),
        "asked": int(approvals["asked"] or 0), "approved": int(approvals["approved"] or 0),
        "rejected": int(approvals["rejected"] or 0), "repeats_absorbed": repeats, "invented_caught": caught,
    }


async def _outcomes() -> dict[str, int]:
    row = await db.fetchrow(
        "select count(*) filter (where status = any($1::text[])) as submitted, "
        "count(*) filter (where status in ('interview', 'offer')) as interviews from job_applications",
        list(SUBMITTED),
    )
    return {"submitted": int(row["submitted"] or 0), "interviews": int(row["interviews"] or 0)}


async def _cost() -> dict[str, Any]:
    today = datetime.now(UTC).date()
    totals: list[Decimal] = []
    for i in range(TEST_DAYS):
        totals.append((await daily_spend(today - timedelta(days=i)))["total"])
    avg = sum(totals, Decimal("0")) / TEST_DAYS
    return {"avg_per_day": avg.quantize(Decimal("0.01")), "days": TEST_DAYS}


def _eval_cases() -> int:
    n = 0
    for p in EVAL_DATA.glob("*.jsonl") if EVAL_DATA.is_dir() else []:
        try:
            n += sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            continue
    return n


async def _connected() -> list[str]:
    rows = await db.fetch("select source from connector_state where last_ok_at is not null order by source")
    return [r["source"] for r in rows]


async def scorecard() -> dict[str, Any]:
    """The eight measures, each with its live value or an honest 'not yet'."""
    # One clock (the database's) for both the stamp and the day count, so a
    # few seconds of skew between Postgres and this process never reads as
    # day 0 or day 2.
    clock = await db.fetchrow(
        "select last_at, greatest(1, least($2::int, floor(extract(epoch from now() - last_at) / 86400)::int + 1)) as day "
        "from system_heartbeats where kind = $1",
        STARTED_KIND, TEST_DAYS,
    )
    started = clock["last_at"] if clock else None
    day = int(clock["day"]) if clock else None

    from core.night import yield_so_far
    from core.outcomes import summary as outcomes_summary

    yld = await yield_so_far()
    trust = await _trust()
    out = await _outcomes()
    cost = await _cost()
    cases = _eval_cases()
    learn = await outcomes_summary()
    connected = await _connected()
    senses = [s for s in ("gmail", "gcal", "canvas") if s in connected]

    rows = [
        _row("overnight_yield", "Overnight yield", "80% of night-shift tasks done to their definition, no rework",
             value=(f"{yld['accepted']} of {yld['reviewed']} accepted" if yld["reviewed"] else "not yet"),
             status=("none" if not yld["reviewed"] else
                     "pass" if yld["accepted"] / yld["reviewed"] >= 0.8 else "fail"),
             detail=("No night-shift result judged yet: approve tonight's tasks, then Accept or Rework each result in the morning."
                     if not yld["reviewed"] else "Your Accept / Rework taps on night-shift results.")),
        _row("leverage", "Leverage", "30 minutes of finished work per minute of your attention",
             value="not yet", status="none", detail="Needs attention-time tracking on approval cards (phase 3)."),
        _row("zero_misses", "Zero misses", "No deadline, bill, grade or professor message unseen for over an hour",
             value="not yet", status="none",
             detail=("Needs Gmail, Calendar and Canvas connected (scripts\\connect.bat)." if not senses
                     else f"Connected: {', '.join(senses)}. Measuring starts with the first due date they report.")),
        _row("trust", "Trust", "Zero outward actions without approval, zero duplicate sends, zero invented claims",
             value=f"{trust['unapproved']} unapproved",
             status="pass" if trust["unapproved"] == 0 else "fail",
             detail=(f"{trust['gated_calls']} gated call{'s' if trust['gated_calls'] != 1 else ''} went through approval; "
                     f"{trust['asked']} card{'s' if trust['asked'] != 1 else ''} asked ({trust['approved']} yes, {trust['rejected']} no); "
                     f"{trust['repeats_absorbed']} retr{'ies' if trust['repeats_absorbed'] != 1 else 'y'} absorbed by the ledger; "
                     f"{trust['invented_caught']} invented claim{'s' if trust['invented_caught'] != 1 else ''} caught before sending. "
                     "Cards approved unchanged: not tracked yet.")),
        _row("outcomes", "Outcomes", "15% of applications reach an interview; client replies within a business day",
             value=(f"{out['interviews']} of {out['submitted']}" if out["submitted"] else "0 sent"),
             status=("none" if out["submitted"] < 5 else
                     "pass" if out["interviews"] / out["submitted"] >= 0.15 else "fail"),
             detail=("Too few applications to judge (needs 5). " if out["submitted"] < 5 else "")
                    + "Reply tracking needs Gmail connected (PR F)."),
        _row("cost", "Cost", "Under $2 a night on average, every dollar traced to a run",
             value=f"${cost['avg_per_day']}/day",
             status="pass" if cost["avg_per_day"] < Decimal("2") else "fail",
             detail=f"Average over the last {cost['days']} days. Every dollar is on a run or a named one-shot call."),
        _row("learning", "Learning", "Every bad outcome becomes an eval case; no mistake happens twice",
             value=(f"{cases} eval cases · {learn['candidates']['proposed']} miss"
                    f"{'es' if learn['candidates']['proposed'] != 1 else ''} waiting"),
             status=("none" if not (learn["this_week"]["good"] + learn["this_week"]["bad"]) else
                     "pass" if learn["candidates"]["proposed"] == 0 else "fail"),
             detail=(f"{learn['this_week']['good']} good and {learn['this_week']['bad']} bad outcome"
                     f"{'s' if learn['this_week']['good'] + learn['this_week']['bad'] != 1 else ''} this week "
                     f"(last week {learn['last_week']['good']} and {learn['last_week']['bad']}). Every bad one proposes an eval "
                     f"case; {learn['candidates']['written']} written so far. A pass means nothing is waiting to be written. "
                     "The repeat check comes with the weekly review.")),
        _row("already_done", "\"Already done\" moments", "Three or more a week",
             value="not yet", status="none", detail="Needs the one-tap \"this surprised me\" on results (phase 7)."),
    ]
    measured = [r for r in rows if r["status"] != "none"]
    return {
        "started": started.isoformat() if started else None,
        "day": day,
        "days": TEST_DAYS,
        "measured": len(measured),
        "passing": sum(1 for r in measured if r["status"] == "pass"),
        "total": len(rows),
        "rows": rows,
    }


__all__ = ["TEST_DAYS", "scorecard", "start_test"]
