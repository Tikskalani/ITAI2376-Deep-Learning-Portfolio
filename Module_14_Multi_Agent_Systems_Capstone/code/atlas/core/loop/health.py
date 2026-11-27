"""Atlas -- self-monitoring.

A read-only health and cost snapshot built from the episodes table. The
evening brief carries one line of it, so Trilok can see what Atlas cost
and whether it is healthy without digging through logs. A system that
reports its own cost and health is one you can trust to run unattended.

Token counts are exact. The dollar figure is a rough estimate: episodes
do not record which model ran, so cost uses a blended approximate rate.
Treat it as a ballpark for catching runaway usage, not an invoice.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from core import db
from core.config import settings
from core.heartbeats import all_heartbeats

log = logging.getLogger(__name__)

# Rough blended USD per token for a ballpark daily cost. Atlas runs Opus
# for the CEO and briefs and Haiku for the loop; these rates split the
# difference. Update if pricing or the model mix shifts materially.
_USD_PER_INPUT_TOKEN = 9.0 / 1_000_000
_USD_PER_OUTPUT_TOKEN = 45.0 / 1_000_000


def _today_start() -> datetime:
    """Midnight today in the configured timezone."""
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def daily_health() -> dict[str, Any]:
    """Summarize today's agent activity, token use, and errors."""
    rows = await db.fetch(
        """
        select what_changed, tags
        from episodes
        where started_at >= $1
        """,
        _today_start(),
    )

    runs = len(rows)
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    errors = 0
    for r in rows:
        what_changed = r["what_changed"]
        tokens = (
            what_changed.get("tokens") or {}
            if isinstance(what_changed, dict)
            else {}
        )
        input_tokens += int(tokens.get("input_tokens", 0) or 0)
        output_tokens += int(tokens.get("output_tokens", 0) or 0)
        cache_tokens += int(tokens.get("cache_read_input_tokens", 0) or 0)
        cache_tokens += int(tokens.get("cache_creation_input_tokens", 0) or 0)
        if "error" in (r["tags"] or []):
            errors += 1

    est_cost = (
        input_tokens * _USD_PER_INPUT_TOKEN
        + output_tokens * _USD_PER_OUTPUT_TOKEN
    )
    return {
        "runs": runs,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "errors": errors,
        "est_cost_usd": round(est_cost, 2),
    }


def health_line(health: dict[str, Any]) -> str:
    """Render a daily_health dict as one human line for a brief."""
    total_tokens = health["input_tokens"] + health["output_tokens"]
    parts = [
        f"{health['runs']} agent run(s)",
        f"{total_tokens:,} tokens",
        f"about ${health['est_cost_usd']:.2f} (rough estimate)",
    ]
    if health["errors"]:
        parts.append(f"{health['errors']} error(s), worth checking the logs")
    else:
        parts.append("no errors")
    return "System health today: " + ", ".join(parts) + "."


async def daily_health_line() -> str:
    """Compute today's health and render it as one line."""
    return health_line(await daily_health())



# ---------------------------------------------------------------------------
# Worker liveness and proactive alerting
# ---------------------------------------------------------------------------
#
# The alert job runs in the web process (core/web/api.py starts it), not in
# the worker it watches: a stalled worker cannot report its own stall. The
# worker writes "worker/started" when it boots and "loop/executive" after
# every tick; anything that reads those two can tell a dead loop from a
# freshly restarted one.

# In-memory cooldown so a persistent issue does not re-page every poll.
_LAST_ALERTED: dict[str, float] = {}
_ALERT_COOLDOWN_S = 4 * 3600  # alert at most every 4 hours per issue kind
# Issues alerted and not yet resolved, so recovery can be announced once.
_OPEN: dict[str, float] = {}
# Consecutive passes each issue has been seen; it pages at CONFIRM_PASSES.
_SEEN: dict[str, int] = {}
CONFIRM_PASSES = 2
DB_RETRY_S = 5.0
# A watchdog sleep that took this many times longer than asked means the
# machine was suspended: the next pass is skipped while things wake up.
WAKE_FACTOR = 2.0

# A worker ticks a minute after it starts (core/loop/scheduler.py); one that has
# not ticked after this long, waiting on Postgres included, is not starting.
FIRST_TICK_MIN = 15


def stale_after_min() -> float:
    """Minutes without a tick after which the loop counts as stalled."""
    return settings.loop_interval_min * 2 + 5


def worker_status(beats: dict[str, datetime], now: datetime | None = None) -> dict[str, Any]:
    """Freshness of the executive loop from the heartbeats.

    fresh is True when the loop ticked within stale_after_min(), or when the
    worker started more recently than its last tick and is still inside its
    first tick's window (it ticks a minute after it starts).

    The restart is checked first: a tick older than the current worker
    belongs to the previous process, so it cannot prove this one works.
    ticked_since_start says whether the running worker has ticked yet.
    """
    now = now or datetime.now(UTC)
    last = beats.get("loop/executive")
    started = beats.get("worker/started")
    limit = stale_after_min()
    out: dict[str, Any] = {
        "last_tick": last.isoformat() if last else None,
        "started": started.isoformat() if started else None,
        "age_min": None,
        "fresh": False,
        "state": "never",
        "ticked_since_start": bool(last and (started is None or last >= started)),
    }
    if last is not None:
        out["age_min"] = round((now - last).total_seconds() / 60, 1)
    if started is not None and (last is None or started > last):
        since = (now - started).total_seconds() / 60
        if since <= FIRST_TICK_MIN:
            out.update(fresh=True, state="starting")
        else:
            out["state"] = "stalled"
        return out
    if last is not None:
        age = (now - last).total_seconds() / 60
        out["age_min"] = round(age, 1)
        if age <= limit:
            out.update(fresh=True, state="ticking")
            return out
        out["state"] = "stalled"
    if started is not None and (last is None or started > last):
        if (now - started).total_seconds() / 60 <= limit:
            out.update(fresh=True, state="starting")
        elif last is None:
            out["state"] = "stalled"
    return out


def _local(ts: datetime) -> str:
    try:
        return ts.astimezone(ZoneInfo(settings.timezone)).strftime("%a %H:%M")
    except Exception:  # noqa: BLE001 - a bad tz setting must not break alerts
        return ts.strftime("%a %H:%M UTC")


async def health_check() -> list[dict[str, str]]:
    """Return the current health issues. Empty list means all clear.

    Each issue has a `kind` (for dedup) and a one-line `detail` that says
    what is wrong in plain words. Cheap and read-only.
    """
    issues: list[dict[str, str]] = []
    now = datetime.now(UTC)

    try:
        beats = await all_heartbeats()
    except Exception as exc:
        # If we can't read heartbeats, the DB is the problem.
        issues.append(
            {
                "kind": "db/heartbeats",
                "detail": f"could not read system_heartbeats: {type(exc).__name__}: {exc}",
            }
        )
        return issues

    status = worker_status(beats, now)
    if status["state"] == "stalled":
        last = beats.get("loop/executive")
        if last is not None:
            mins = int((now - last).total_seconds() / 60)
            detail = (
                f"The background loop has not run for {mins} minutes (last at {_local(last)}). "
                "Briefs, reminders and mail checks are paused until it does."
            )
        else:
            detail = "The background loop started but has never completed a run."
        issues.append({"kind": "loop/executive", "detail": detail})

    # Postgres reachability: a select 1 round trip, retried once. After a wake
    # Docker's Postgres answers within seconds; that is not an outage.
    try:
        try:
            await db.fetchval("select 1")
        except Exception:
            import asyncio

            await asyncio.sleep(DB_RETRY_S)
            await db.fetchval("select 1")
    except Exception as exc:
        issues.append(
            {
                "kind": "db/connection",
                "detail": f"postgres is unreachable: {type(exc).__name__}: {exc}",
            }
        )

    return issues


def _format_alert(issues: list[dict[str, str]]) -> str:
    lines = ["Atlas health alert. Something has gone quiet:"]
    for issue in issues:
        lines.append(f"  - {issue['detail']}")
    return "\n".join(lines)


def _title(issues: list[dict[str, str]]) -> str:
    """The first problem in a few words, so the card says what broke."""
    kinds = {i["kind"] for i in issues}
    if "loop/executive" in kinds:
        head = "Atlas loop stalled"
    elif kinds & {"db/connection", "db/heartbeats"}:
        head = "Atlas database unreachable"
    else:
        head = "Atlas health alert"
    return head if len(issues) == 1 else f"{head} (+{len(issues) - 1} more)"


async def health_alert_job() -> None:
    """Run health_check, page the owner about anything new, and say once
    when an alerted problem clears.

    Each issue kind alerts at most every _ALERT_COOLDOWN_S seconds.
    """
    try:
        issues = await health_check()
    except Exception:
        log.exception("health_check raised")
        return

    # Lazy import keeps this module importable without the surface layer.
    from core.surfaces.notify import notify

    now = time.time()
    current = {i["kind"] for i in issues}
    for kind in [k for k in _OPEN if k not in current]:
        since = _OPEN.pop(kind)
        mins = int((now - since) / 60)
        label = "The background loop" if kind == "loop/executive" else "The database"
        try:
            await notify(
                kind="health",
                title="Atlas recovered",
                body=f"{label} is working again (alerted {mins} minutes ago). Missed reminders run now, marked overdue.",
                priority="normal",
            )
        except Exception:
            log.exception("could not deliver recovery notice")

    # Page only for an issue seen on two passes in a row: one pass right after a
    # wake races the worker's catch-up tick and Postgres coming back.
    for kind in [k for k in _SEEN if k not in current]:
        _SEEN.pop(kind)
    fresh: list[dict[str, str]] = []
    for issue in issues:
        _SEEN[issue["kind"]] = _SEEN.get(issue["kind"], 0) + 1
        if _SEEN[issue["kind"]] < CONFIRM_PASSES:
            continue
        last = _LAST_ALERTED.get(issue["kind"], 0.0)
        if now - last >= _ALERT_COOLDOWN_S:
            fresh.append(issue)
            _LAST_ALERTED[issue["kind"]] = now
            _OPEN.setdefault(issue["kind"], now)
    if not fresh:
        return

    try:
        await notify(kind="health", title=_title(fresh), body=_format_alert(fresh), priority="high")
    except Exception:
        log.exception("could not deliver health alert to owner")


async def watch_forever(interval_s: float = 300.0, first_delay_s: float = 120.0) -> None:
    """The web process's watchdog: health_alert_job every interval_s.
    Runs until cancelled; never raises otherwise."""
    import asyncio

    await asyncio.sleep(first_delay_s)
    while True:
        try:
            await health_alert_job()
        except Exception:  # noqa: BLE001 - the watchdog must outlive any one failure
            log.exception("health watchdog pass failed")
        before = time.time()
        await asyncio.sleep(interval_s)
        if time.time() - before > interval_s * WAKE_FACTOR:
            # The laptop slept through this sleep. Give the worker its catch-up
            # tick and Postgres its start before judging either.
            log.info("health watchdog: woke after %.0f min; skipping one pass", (time.time() - before) / 60)
            await asyncio.sleep(interval_s)


__all__ = [
    "daily_health",
    "daily_health_line",
    "health_alert_job",
    "health_check",
    "health_line",
    "stale_after_min",
    "watch_forever",
    "worker_status",
]
