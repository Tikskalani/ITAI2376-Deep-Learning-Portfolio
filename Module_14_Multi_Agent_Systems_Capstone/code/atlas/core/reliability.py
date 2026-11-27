"""Atlas -- reliability: why a brief was late, and whether Atlas kept its promises.

Atlas runs on a laptop that sleeps. Until 2026-10-21 three things turned a
sleep into a late or missing brief: the liveness switch counted the sleep as
a blocked event loop and killed the worker on wake; the new worker crashed
when Docker had not brought Postgres back yet; and a worker that came back
after a brief's slot only caught up once, at start. This module holds the
pieces that close those gaps and make the rest visible:

  wait_for_db      the worker waits for Postgres at boot instead of crashing
                   (NSSM used to restart it every two minutes for an hour)
  record_start /   a short history of worker starts and machine sleeps
  record_sleep
  late_reason      one plain sentence on why a brief missed its slot
  tick_gap         the worst gap between executive ticks today
  run_self_check   the nightly SLO check: each brief delivered within
                   BRIEF_SLO_MIN of its slot, the loop's heartbeat never
                   older than stale_after_min()
  slo_summary      the last result and the pass rate, for /api/health and
                   the Bridge
  code_revision    the commit on disk, so a web app running older code than
                   the checkout says so instead of answering 404

No model calls anywhere in here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from core import db
from core.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

BRIEF_SLO_MIN = 15
HISTORY_KIND = "worker/history"
_HISTORY_KEEP = 20
SLO_DAYS = 7


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _hm(ts: datetime) -> str:
    return ts.astimezone(_tz()).strftime("%H:%M")


# ------------------------------------------------------------------ boot


async def wait_for_db(
    max_wait_s: float = 900.0,
    *,
    connect: Callable[[], Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Open the pool, retrying while Postgres is not up yet. Returns the
    number of failed attempts; raises the last error after max_wait_s."""
    connect = connect or db.get_pool
    waited, delay, failures = 0.0, 2.0, 0
    while True:
        try:
            await connect()
            if failures:
                log.info("postgres answered after %d attempt(s), %.0f s", failures + 1, waited)
            return failures
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
            failures += 1
            if waited >= max_wait_s:
                log.error("postgres still unreachable after %.0f s; giving up", waited)
                raise
            log.warning("postgres not reachable yet (%s); retrying in %.0f s", type(exc).__name__, delay)
            await sleep(delay)
            waited += delay
            delay = min(delay * 2, 30.0)


# ------------------------------------------------------------------ history


async def _history() -> dict[str, list[Any]]:
    row = await db.fetchrow("select metadata from system_heartbeats where kind = $1", HISTORY_KIND)
    meta = row["metadata"] if row and isinstance(row["metadata"], dict) else {}
    return {"starts": list(meta.get("starts") or []), "sleeps": list(meta.get("sleeps") or [])}


async def _remember(key: str, item: Any) -> None:
    hist = await _history()
    hist[key] = (hist[key] + [item])[-_HISTORY_KEEP:]
    await db.execute(
        """
        insert into system_heartbeats (kind, last_at, metadata) values ($1, now(), $2)
        on conflict (kind) do update set last_at = excluded.last_at, metadata = excluded.metadata
        """,
        HISTORY_KIND,
        hist,
    )


async def record_start(at: datetime | None = None) -> None:
    await _remember("starts", (at or datetime.now(UTC)).isoformat())


async def record_sleep(asleep_from: datetime, asleep_to: datetime) -> None:
    """liveness.start's on_resume hook: the sleep goes into the history."""
    await _remember("sleeps", [asleep_from.isoformat(), asleep_to.isoformat()])


def _explain(slot_at: datetime, until: datetime, hist: dict[str, list[Any]]) -> str | None:
    """Why nothing ran at slot_at, from the history; None when it has no answer."""
    for frm_s, to_s in reversed(hist["sleeps"]):
        frm, to = datetime.fromisoformat(frm_s), datetime.fromisoformat(to_s)
        if frm <= slot_at < to:
            return f"the laptop was asleep from {_hm(frm)} to {_hm(to)}"
    starts = sorted(datetime.fromisoformat(s) for s in hist["starts"])
    after = [s for s in starts if slot_at < s <= until]
    if after:
        return f"Atlas was not running at {_hm(slot_at)} (the worker started at {_hm(after[0])})"
    return None


async def late_reason(slot_at: datetime, now: datetime | None = None) -> str:
    """One clause on why a brief due at slot_at is running late."""
    try:
        hist = await _history()
    except Exception:  # noqa: BLE001 - a reason is never worth failing a brief
        log.exception("could not read the worker history")
        hist = {"starts": [], "sleeps": []}
    return _explain(slot_at, now or datetime.now(UTC), hist) or "Atlas was not running then"


# ------------------------------------------------------------------ heartbeat gaps


async def tick_gap(now: datetime | None = None) -> dict[str, Any]:
    """{"gaps": {"day", "max_gap_min"}} for the executive heartbeat: the
    longest wait between two ticks that ended today (local)."""
    now = now or datetime.now(UTC)
    day = now.astimezone(_tz()).date().isoformat()
    row = await db.fetchrow("select last_at, metadata from system_heartbeats where kind = 'loop/executive'")
    if row is None:
        return {"gaps": {"day": day, "max_gap_min": 0.0}}
    gap = max(0.0, (now - row["last_at"]).total_seconds() / 60)
    prev = (row["metadata"] or {}).get("gaps") if isinstance(row["metadata"], dict) else None
    if isinstance(prev, dict) and prev.get("day") == day:
        gap = max(gap, float(prev.get("max_gap_min") or 0))
    return {"gaps": {"day": day, "max_gap_min": round(gap, 1)}}


# ------------------------------------------------------------------ the SLO


def check_day(now: datetime) -> date:
    """The day a self-check judges: today at 23:50, yesterday when the
    laptop slept through the check and it runs on wake before 06:00."""
    return (now.astimezone(_tz()) - timedelta(hours=6)).date()


def _slot(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=_tz())


async def _brief_result(kind: str, day: date, now: datetime, hist: dict[str, list[Any]]) -> dict[str, Any] | None:
    slot = _slot(day, settings.brief_morning if kind == "morning" else settings.brief_evening)
    if now < slot + timedelta(minutes=BRIEF_SLO_MIN):
        return None  # not judged yet
    delivered = await db.fetchval(
        "select min(created_at) from briefs where kind = $1 and created_at >= $2 and created_at < $3",
        kind, slot - timedelta(hours=1), _slot(day + timedelta(days=1), time(0)),
    )
    target = f"delivered within {BRIEF_SLO_MIN} min of {_hm(slot)}"
    if delivered is None:
        why = _explain(slot, now, hist)
        return {"objective": f"brief/{kind}", "ok": False, "value": None, "target": BRIEF_SLO_MIN,
                "detail": f"No {kind} brief was delivered" + (f"; {why}." if why else ".")}
    late = max(0.0, (delivered - slot).total_seconds() / 60)
    ok = late <= BRIEF_SLO_MIN
    detail = f"Delivered at {_hm(delivered)}, {int(late)} min after {_hm(slot)}"
    if not ok:
        why = _explain(slot, delivered, hist)
        detail += f"; {why}." if why else "; no sleep or restart was recorded."
    else:
        detail += f" ({target})."
    return {"objective": f"brief/{kind}", "ok": ok, "value": round(late, 1), "target": BRIEF_SLO_MIN, "detail": detail}


async def _heartbeat_result(day: date, now: datetime, hist: dict[str, list[Any]]) -> dict[str, Any]:
    from core.loop.health import stale_after_min

    limit = stale_after_min()
    row = await db.fetchrow("select last_at, metadata from system_heartbeats where kind = 'loop/executive'")
    if row is None:
        return {"objective": "heartbeat", "ok": False, "value": None, "target": limit,
                "detail": "The background loop has never ticked."}
    gaps = (row["metadata"] or {}).get("gaps") if isinstance(row["metadata"], dict) else None
    worst = float(gaps["max_gap_min"]) if isinstance(gaps, dict) and gaps.get("day") == day.isoformat() else 0.0
    if now.astimezone(_tz()).date() == day:
        worst = max(worst, (now - row["last_at"]).total_seconds() / 60)
    ok = worst <= limit
    detail = f"Longest wait between loop ticks: {int(worst)} min (limit {int(limit)})."
    if not ok:
        slept = [s for s in hist["sleeps"] if datetime.fromisoformat(s[0]).astimezone(_tz()).date() == day
                 or datetime.fromisoformat(s[1]).astimezone(_tz()).date() == day]
        if slept:
            frm, to = (datetime.fromisoformat(t) for t in slept[-1])
            detail += f" The laptop slept from {_hm(frm)} to {_hm(to)}."
    return {"objective": "heartbeat", "ok": ok, "value": round(worst, 1), "target": limit, "detail": detail}


async def run_self_check(now: datetime | None = None) -> list[dict[str, Any]]:
    """Judge one day against the SLO and store the verdicts. Never calls a model."""
    now = now or datetime.now(UTC)
    day = check_day(now)
    hist = await _history()
    results = [r for r in [await _brief_result("morning", day, now, hist),
                           await _brief_result("evening", day, now, hist),
                           await _heartbeat_result(day, now, hist)] if r is not None]
    for r in results:
        await db.execute(
            """
            insert into slo_checks (day, objective, ok, value, target, detail)
            values ($1, $2, $3, $4, $5, $6)
            on conflict (day, objective) do update
               set ok = excluded.ok, value = excluded.value, target = excluded.target,
                   detail = excluded.detail, checked_at = now()
            """,
            day, r["objective"], r["ok"], r["value"], r["target"], r["detail"],
        )
        r["day"] = day.isoformat()
    log.info("slo self-check for %s: %s", day, ", ".join(f"{r['objective']}={'ok' if r['ok'] else 'missed'}" for r in results))
    return results


SELF_CHECK_TRIES = 3
SELF_CHECK_WAIT_S = 30.0


async def scheduled_self_check(wait_s: float = SELF_CHECK_WAIT_S) -> None:
    """Scheduler entry. Never raises. A database blip retries: on 2026-10-22
    the only 23:50 check failed on a dropped connection and the day was left
    with no verdict at all ("0 of 0" in health)."""
    for attempt in range(1, SELF_CHECK_TRIES + 1):
        try:
            await run_self_check()
            return
        except Exception:  # noqa: BLE001
            log.exception("slo self-check failed (attempt %d of %d)", attempt, SELF_CHECK_TRIES)
            if attempt < SELF_CHECK_TRIES:
                await asyncio.sleep(wait_s)


_SELF_CHECKED: set[date] = set()


async def catch_up_self_check(now: datetime | None = None) -> date | None:
    """Judge yesterday if it has no verdict yet: the 23:50 check failed, or the
    worker was down or asleep through it and woke after 06:00. A brief that
    never went out then counts as a miss instead of "no data" (audit finding
    14). Once per day per worker; returns the day judged."""
    now = (now or datetime.now(UTC)).astimezone(_tz())
    yesterday = now.date() - timedelta(days=1)
    if yesterday in _SELF_CHECKED:
        return None
    if await db.fetchval("select exists (select 1 from slo_checks where day = $1)", yesterday):
        _SELF_CHECKED.add(yesterday)
        return None
    _SELF_CHECKED.add(yesterday)
    # Judged as of the end of that day: check_day(23:59) is that day.
    await run_self_check(datetime.combine(yesterday, time(23, 59), tzinfo=_tz()))
    return yesterday


_LABELS = {"brief/morning": "Morning brief on time", "brief/evening": "Evening brief on time",
           "heartbeat": "Loop heartbeat fresh"}


async def _today_briefs(today: date, now: datetime, stored: set[str]) -> list[dict[str, Any]]:
    """Today's briefs judged now, for the ones the 23:50 check has not stored yet:
    a brief already past its window counts the moment it is late, not at midnight."""
    hist = await _history()
    out = []
    for kind in ("morning", "evening"):
        if f"brief/{kind}" in stored:
            continue
        r = await _brief_result(kind, today, now, hist)
        if r is not None:
            out.append({**r, "day": today})
    return out


async def slo_summary(days: int = SLO_DAYS, now: datetime | None = None) -> dict[str, Any]:
    """Per objective: the latest verdict and how many of the last `days` passed.
    Days are Houston days; today's briefs are judged live (see _today_briefs)."""
    now = now or datetime.now(UTC)
    today = now.astimezone(_tz()).date()
    rows = [dict(r) for r in await db.fetch(
        "select day, objective, ok, value, target, detail from slo_checks "
        "where day > $2::date - $1::int order by day desc",
        days, today,
    )]
    stored_today = {r["objective"] for r in rows if r["day"] == today}
    rows = await _today_briefs(today, now, stored_today) + rows
    out: list[dict[str, Any]] = []
    for objective, label in _LABELS.items():
        mine = [r for r in rows if r["objective"] == objective]
        last = mine[0] if mine else None
        out.append({
            "objective": objective, "label": label,
            "passed": sum(1 for r in mine if r["ok"]), "total": len(mine),
            "last": None if last is None else {"day": last["day"].isoformat(), "ok": last["ok"],
                                                "value": float(last["value"]) if last["value"] is not None else None,
                                                "detail": last["detail"]},
        })
    judged = [o for o in out if o["last"]]
    return {"days": days, "brief_within_min": BRIEF_SLO_MIN, "objectives": out,
            "ok": all(o["last"]["ok"] for o in judged) if judged else None}


# ------------------------------------------------------------------ code on disk


def code_revision(root: Path = REPO_ROOT) -> str | None:
    """The commit checked out at root, read from .git without running git.
    None when it cannot be told (no .git, a detached oddity)."""
    try:
        git = root / ".git"
        if git.is_file():  # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12]
        ref = head[4:].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:12]
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:12]
    except (OSError, IndexError):
        return None
    return None


__all__ = [
    "BRIEF_SLO_MIN", "check_day", "code_revision", "late_reason", "record_sleep", "record_start",
    "catch_up_self_check", "run_self_check", "scheduled_self_check", "slo_summary", "tick_gap", "wait_for_db",
]
