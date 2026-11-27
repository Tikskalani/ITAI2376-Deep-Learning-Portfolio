"""The day on one call, for the Bridge's dial.

The dial is a 24-hour clock: the last 24 hours of finished work behind the
NOW needle, the next 24 hours of the plan ahead of it. day() composes both
from what Atlas already keeps (the event stream and the Planner's agenda)
and writes nothing.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from core import events, planner
from core.config import settings
from core.night import night_of, window

# What counts as "done" on the dial. night.task and recording also fire while
# work is still under way; only their finished states are kept.
DONE_KINDS: tuple[str, ...] = (
    "run.finished", "approval.finished", "submit.status", "night.task", "recording", "news.brief",
)
_FINISHED = {"night.task": {"done", "needs_you"}, "recording": {"done"}}
_ALL_DAY_AT = time(9, 0)
_NIGHT_SLOTS = ((time(22, 30), "Night shift proposes tonight's work"), (time(23, 30), "Night shift runs"))


def _parse(at: Any) -> datetime | None:
    if isinstance(at, datetime):
        return at
    if isinstance(at, str) and at:
        try:
            return datetime.fromisoformat(at)
        except ValueError:
            return None
    return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal("0")


def _finished(ev: dict[str, Any]) -> bool:
    wanted = _FINISHED.get(ev.get("kind") or "")
    return wanted is None or (ev.get("data") or {}).get("status") in wanted


async def day(now: datetime | None = None) -> dict[str, Any]:
    """The last 24 hours of finished work and the next 24 hours of the plan."""
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    start, horizon = now - timedelta(hours=24), now + timedelta(hours=24)

    done: list[dict[str, Any]] = []
    cost = Decimal("0")
    runs: set[str] = set()
    kept = [ev for ev in await events.recent(limit=200, kinds=list(DONE_KINDS), start=start, end=now)
            if (at := _parse(ev.get("at"))) is not None and start <= at <= now and _finished(ev)]
    # A night-shift task reports twice (night.task and its run.finished); the
    # dial shows the task, in his words, and the run still counts its cost.
    night_runs = {ev.get("episode_id") for ev in kept if ev.get("kind") == "night.task" and ev.get("episode_id")}
    for ev in kept:
        at = _parse(ev.get("at"))
        assert at is not None
        if ev.get("kind") == "run.finished":
            cost += _money((ev.get("data") or {}).get("cost"))
            runs.add(str(ev.get("episode_id") or ev.get("id")))
            if ev.get("episode_id") in night_runs:
                continue
        done.append({"at": at.astimezone(tz).isoformat(), "kind": ev["kind"], "agent": ev.get("agent"),
                     "title": ev.get("title") or "", "episode_id": ev.get("episode_id")})

    nxt: list[tuple[datetime, dict[str, Any]]] = []
    plan = await planner.agenda(now.date(), 2)
    for bucket in plan.get("days", []):
        for item in bucket.get("items", []):
            parsed = _parse(item.get("at"))
            all_day = parsed is None
            at = parsed or datetime.combine(bucket["date"], _ALL_DAY_AT, tzinfo=tz)
            if now < at <= horizon:
                nxt.append((at, {"at": at.astimezone(tz).isoformat(), "kind": item.get("kind") or "task",
                                 "title": item.get("title") or "", "all_day": all_day}))
    for offset in (0, 1):
        d = now.date() + timedelta(days=offset)
        for slot, title in _NIGHT_SLOTS:
            at = datetime.combine(d, slot, tzinfo=tz)
            if now < at <= horizon:
                nxt.append((at, {"at": at.isoformat(), "kind": "night", "title": title, "all_day": False}))
    nxt.sort(key=lambda pair: pair[0])

    shift_start, shift_end = window(night_of(now))
    stamps = [_parse(d["at"]) for d in done]
    in_shift = [t for t in stamps if t is not None and shift_start <= t <= shift_end]
    hours = round((max(in_shift) - min(in_shift)).total_seconds() / 3600, 1) if len(in_shift) > 1 else 0.0

    return {
        "now": now.isoformat(),
        "tz": settings.timezone,
        "night": {"start": shift_start.isoformat(), "end": shift_end.isoformat()},
        "done": done,
        "next": [item for _, item in nxt],
        "totals": {"done": len(done), "runs": len(runs), "cost": str(cost.quantize(Decimal("0.01"))),
                   "hours": hours},
    }
