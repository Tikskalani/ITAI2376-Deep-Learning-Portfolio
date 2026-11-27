"""Atlas -- briefs and the weekly review.

Scheduled jobs that run the CEO and deliver its output to Trilok through
the notification layer (push to the PWA, email fallback). The CEO's
system prompt defines the formats; this module gives it the cue, the
date, the context, and a delivery path.

  * run_brief         -- the morning and evening briefs
  * run_weekly_review -- the Sunday week-behind, week-ahead review

Every brief is stored in the briefs table before it is pushed, so the
PWA can show history and a missed push loses nothing. The morning and
evening briefs receive what arrived since the last brief (the triaged
signals) as context and are told to lead with it and to propose at most
five sized items. The evening brief also carries the system-health and
cost lines.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from core import db
from core.config import settings
from core.costs import daily_cost_line
from core.decisions import decisions_due_for_review, list_decisions
from core.decisions_review import send_review_round
from core.heartbeats import set_heartbeat
from core.llm import describe_api_error, is_transient
from core.loop.health import daily_health_line
from core.runtime.dispatcher import dispatch
from core.signals import brief_block, private_line
from core.surfaces.notify import notify

log = logging.getLogger(__name__)

# Waits between attempts when a brief fails for a reason that passes (no
# network right after the laptop wakes, Anthropic overloaded). About 42
# minutes end to end; a bad key or an empty balance is reported at once.
RETRY_DELAYS_S: tuple[float, ...] = (120.0, 600.0, 1800.0)

# Hours of signals each brief looks back over.
_SIGNAL_WINDOW = {"morning": 24, "evening": 12, "weekly": 24 * 7}
_MAX_ITEMS = 5


def _today_str() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%A, %B %d, %Y")


def _brief_prompt(kind: str, late: str = "", reason: str = "") -> str:
    """The instruction handed to the CEO for a morning or evening brief."""
    when = "morning" if kind == "morning" else "evening"
    lateness = (
        f"This brief is running late: it was due at {late} and {reason or 'Atlas was not running then'}. "
        "Say so in one line at the top. "
        if late else ""
    )
    return (
        lateness
        + f"It is the {when} of {_today_str()}. Produce Trilok's {when} "
        "brief, following the brief format in your instructions. The context "
        "above lists what arrived since the last brief; lead with it, most "
        "important first, and say what each item needs from him. Then propose "
        f"at most {_MAX_ITEMS} items for the day, each with a time estimate in "
        "minutes; if they will not fit his free hours, say so and offer what to "
        "drop. Check his active goals and relevant memory with your tools, then "
        "write the brief itself as your final reply. Keep it readable on a phone. "
        "The promises Trilok owes are appended after your reply automatically, one line "
        "each; do not repeat them."
    )


def _weekly_prompt() -> str:
    """The instruction handed to the CEO for the weekly review."""
    return (
        f"It is {_today_str()}, the end of the week. Produce Trilok's "
        "weekly review. Cover the week behind (what arrived, where his "
        "projects and goals moved, what was decided) and the week ahead (what "
        "is coming and what needs his attention), then end with one sharp "
        "strategic question worth his thought. Signal and decision-journal "
        "context is provided above; check his goals with your tools. Keep it "
        "readable in two minutes."
    )


async def _signals_context(kind: str) -> str:
    """What arrived in the brief's window, as a block for memory_context.
    The morning brief also carries the job search (funnel, what is due,
    the best new roles)."""
    hours = _SIGNAL_WINDOW.get(kind, 24)
    try:
        block = await brief_block(hours)
    except Exception:
        log.exception("could not build the signals block for the %s brief", kind)
        block = "(signals unavailable)"
    out = f"What arrived in the last {hours} hours:\n{block}"
    if kind == "morning":
        try:
            from core.jobs.followups import brief_block as jobs_block

            jobs = await jobs_block()
            if jobs:
                out += f"\n\n{jobs}"
        except Exception:
            log.exception("could not build the job-search block for the morning brief")
    return out


async def _weekly_context() -> str:
    """Signals plus the decision-journal context block for the weekly review."""
    lines: list[str] = [await _signals_context("weekly"), "", "Decision journal:"]

    open_decisions = await list_decisions(status="open")
    if open_decisions:
        lines.append(f"Open decisions ({len(open_decisions)}):")
        for d in open_decisions[:20]:
            lines.append(f"  - {d['summary']}")
    else:
        lines.append("No open decisions.")

    due = await decisions_due_for_review()
    if due:
        lines.append(f"Decisions due for review ({len(due)}):")
        for d in due[:20]:
            lines.append(f"  - {d['summary']}")

    return "\n".join(lines)


async def _store_brief(kind: str, text: str) -> UUID:
    return await db.fetchval(
        "insert into briefs (kind, body) values ($1, $2) returning id", kind, text
    )


async def _deliver(text: str, kind: str, *, store: bool = True) -> None:
    """Store the brief (unless it is a failure notice) and notify the owner."""
    data: dict[str, Any] = {}
    if store:
        brief_id = await _store_brief(kind, text)
        data["brief_id"] = str(brief_id)
    if store and kind in ("morning", "evening"):
        # Each promise he owes gets a Done button on the brief's card.
        try:
            from core import context_pack

            data["owes"] = [{"key": o["key"], "who": o["who"], "project": o["project"], "text": o["text"]}
                            for o in context_pack.owed()]
        except Exception:
            log.exception("could not attach the owed promises to the brief")
    label = {"morning": "Morning brief", "evening": "Evening brief", "weekly": "Weekly review"}[kind]
    res = await notify(
        kind=f"brief/{kind}",
        title=f"{label}, {_today_str()}",
        body=text,
        priority="normal" if store else "high",
        data=data,
    )
    if store and res is not None:
        await db.execute(
            "update briefs set notification_id = $2 where id = $1",
            UUID(data["brief_id"]),
            res.notification_id,
        )


async def _dispatch_with_retry(label: str, prompt: str, context: str, dispatched_by: str):
    """dispatch('ceo') with spaced retries on transient failures.

    Returns the result, or raises the last exception: at once when the
    failure will not pass on its own, after RETRY_DELAYS_S otherwise."""
    delays = list(RETRY_DELAYS_S)
    while True:
        try:
            return await dispatch(
                "ceo",
                prompt,
                dispatched_by=dispatched_by,
                participants=["owner", "ceo"],
                memory_context=context,
            )
        except Exception as exc:
            if not delays or not is_transient(exc):
                raise
            wait = delays.pop(0)
            log.warning("%s failed (%s); retrying in %.0f s", label, describe_api_error(exc), wait)
            await asyncio.sleep(wait)


async def _failure_notice(label: str, exc: BaseException) -> str:
    reason = describe_api_error(exc)
    tried = "after retrying for about 40 minutes" if is_transient(exc) else "and retrying would not help"
    return f"Atlas could not write the {label} {tried}. Reason: {reason}"


# ---------------------------------------------------------------------------
# The quiet brief: nothing new, no model call (efficiency lever 3b, r12)
# ---------------------------------------------------------------------------
#
# Half the evening briefs in October said, at about $0.30 to $0.54 of Opus
# each, that nothing had happened. When no row that a brief reports on was
# added or changed since the last brief, a short template goes out instead.
# A full brief still goes out at least every QUIET_MAX_AGE, and only with
# ATLAS_BRIEF_SKIP_UNCHANGED=true.

def _owes() -> str:
    """The owed-promises block from the context pack; empty when unreadable."""
    try:
        from core import context_pack

        return context_pack.owes_block()
    except Exception:
        log.exception("could not build the owed-promises block")
        return ""


async def _private(kind: str) -> str:
    """Counts of the window's mail no model read, per rule label (core/privacy.py).
    Appended by code, like the owes block: the model never sees a private
    signal's sender, subject or body, but Trilok still learns it is waiting."""
    try:
        return await private_line(_SIGNAL_WINDOW.get(kind, 24))
    except Exception:
        log.exception("could not count private signals for the %s brief", kind)
        return ""


def _with_owes(text: str) -> str:
    owes = _owes()
    return f"{text}\n\n{owes}" if owes else text


LAST_BRIEF = "brief/last"
QUIET_MAX_AGE = timedelta(hours=48)
# What a brief reports on, as "rows added or changed since then".
_CHANGES: dict[str, str] = {
    "signals": "select count(*) from signals where created_at > $1",
    "approvals": "select count(*) from approvals where created_at > $1 or resolved_at > $1",
    "tasks": "select count(*) from deadlines where created_at > $1 or completed_at > $1",
    "night tasks": "select count(*) from night_tasks where updated_at > $1",
    "goals": "select count(*) from goals where created_at > $1 or closed_at > $1",
    "job events": "select count(*) from job_events where at > $1",
    "recordings": "select count(*) from recordings where updated_at > $1",
}


# The sources whose silence a quiet brief may report, and what each one covers.
# A source counts as checked only when it is connected and synced recently;
# "nothing new" is never said about one that was not looked at.
_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("Gmail", "mail", "gmail"), ("Calendar", "the calendar", "calendar"),
    ("Canvas", "assignments", "canvas"), ("GitHub", "repos", "github"),
)
SOURCE_STALE_AFTER = timedelta(hours=6)


def _joined(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


async def source_coverage(now: datetime) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    """(checked source names, {reason: [(name, what it covers)]}) from the
    connection tiles (no provider call) and each connector's last good sync."""
    from core import connect

    status = await connect.status(check=False)
    google = status["google"]
    tiles = {
        "gmail": (bool(google.get("consented")) and not google.get("needs_reconnect"), google.get("gmail")),
        "calendar": (bool(google.get("consented")) and not google.get("needs_reconnect"), google.get("calendar")),
        "canvas": (bool(status["canvas"].get("configured")), status["canvas"].get("state")),
        "github": (bool(status["github"].get("configured")), status["github"].get("state")),
    }
    checked: list[str] = []
    unchecked: dict[str, list[tuple[str, str]]] = {}
    for name, covers, key in _SOURCES:
        connected, state = tiles[key]
        last_ok = (state or {}).get("last_ok_at")
        if isinstance(last_ok, str):
            last_ok = datetime.fromisoformat(last_ok)
        if not connected:
            reason = "not connected"
        elif last_ok is None:
            reason = "connected but never synced"
        elif now - last_ok > SOURCE_STALE_AFTER:
            reason = "not synced in the last 6 hours"
        else:
            checked.append(name)
            continue
        unchecked.setdefault(reason, []).append((name, covers))
    return checked, unchecked


async def changes_since(since: datetime) -> dict[str, int]:
    """Rows a brief would report on that were added or changed after `since`."""
    return {name: int(await db.fetchval(sql, since) or 0) for name, sql in _CHANGES.items()}


async def _last_brief() -> dict[str, datetime] | None:
    row = await db.fetchrow("select metadata from system_heartbeats where kind = $1", LAST_BRIEF)
    meta = row["metadata"] if row and isinstance(row["metadata"], dict) else None
    if not meta or not meta.get("at") or not meta.get("full_at"):
        return None
    return {"at": datetime.fromisoformat(meta["at"]), "full_at": datetime.fromisoformat(meta["full_at"])}


async def _note_brief(full: bool, now: datetime, last: dict[str, datetime] | None) -> None:
    full_at = now if full or last is None else last["full_at"]
    await set_heartbeat(LAST_BRIEF, {"at": now.isoformat(), "full_at": full_at.isoformat(), "full": full})


async def quiet_brief_text(
    kind: str,
    last_at: datetime,
    now: datetime,
    coverage: tuple[list[str], dict[str, list[tuple[str, str]]]],
) -> str:
    """The template: what was and was not checked, what is due, what waits,
    what it cost. No model."""
    from core import planner

    tz = ZoneInfo(settings.timezone)
    label = "Morning" if kind == "morning" else "Evening"
    checked, unchecked = coverage
    seen = f"no new signals from {_joined(checked)}, " if checked else ""
    lines = [
        f"{label} brief, {now.astimezone(tz):%a %b %d}. Nothing new since the last brief at "
        f"{last_at.astimezone(tz):%H:%M} in what Atlas could check: {seen}no tasks added or finished, "
        "no approvals, no night-shift results."
    ]
    for reason, sources in unchecked.items():
        names = [n for n, _ in sources]
        verb = "is" if len(names) == 1 else "are"
        lines.append(f"Not checked: {_joined(names)} {verb} {reason}, so {_joined([c for _, c in sources])} "
                     "were not looked at.")
    owes = _owes()
    if owes:
        lines.append(owes)
    private = await _private(kind)
    if private:
        lines.append(private)
    try:
        plan = await planner.agenda(now.astimezone(tz).date(), 2)
        agenda = planner.text_agenda(plan)
        lines.append(agenda if agenda else "Nothing is due today or tomorrow.")
    except Exception:
        log.exception("quiet brief: could not read the agenda")
    try:
        waiting = int(await db.fetchval("select count(*) from approvals where status = 'pending'") or 0)
        if waiting:
            lines.append(f"{waiting} approval{'s' if waiting != 1 else ''} still wait{'' if waiting != 1 else 's'} in the Inbox.")
    except Exception:
        log.exception("quiet brief: could not count approvals")
    if kind == "evening":
        try:
            lines.append(await daily_cost_line())
        except Exception:
            log.exception("quiet brief: could not add the cost line")
    lines.append("Written without the model because nothing Atlas could check had changed. Ask in chat for a full brief.")
    return "\n\n".join(lines)


async def _try_quiet(kind: str, now: datetime) -> bool:
    """Send the quiet brief when it applies. True when it went out."""
    if not settings.brief_skip_unchanged:
        return False
    try:
        last = await _last_brief()
        if last is None or now - last["full_at"] >= QUIET_MAX_AGE:
            return False
        changes = await changes_since(last["at"])
        # Which sources were looked at; without that, "nothing new" cannot be said.
        coverage = await source_coverage(now)
    except Exception:
        log.exception("quiet brief: could not check for changes; writing a full brief")
        return False
    if any(changes.values()):
        log.info("%s brief: changes since the last brief (%s); full brief",
                 kind, ", ".join(f"{k} {v}" for k, v in changes.items() if v))
        return False
    await _deliver(await quiet_brief_text(kind, last["at"], now, coverage), kind)
    await _note_brief(False, now, last)
    log.info("%s brief: nothing changed since %s; quiet brief sent, no model call", kind, last["at"].isoformat())
    return True


async def run_brief(kind: str, *, late: str = "", reason: str = "") -> None:
    """Run the CEO for a brief and deliver it. kind is 'morning' or 'evening'.

    Transient failures retry on RETRY_DELAYS_S. Never raises: a brief that
    fails for good is logged and the owner is told why, so one bad brief
    does not take down the scheduler. `late` (e.g. "08:00") marks a brief
    run after Atlas missed its slot; `reason` says why in one clause.
    """
    if kind not in ("morning", "evening"):
        raise ValueError(f"kind must be 'morning' or 'evening', got {kind!r}")

    log.info("running %s brief%s", kind, f" (late; due {late})" if late else "")
    now = datetime.now(UTC)
    try:
        if await _try_quiet(kind, now):
            return
    except Exception:
        log.exception("quiet %s brief failed; writing a full brief", kind)
    try:
        context = await _signals_context(kind)
        result = await _dispatch_with_retry(
            f"{kind} brief", _brief_prompt(kind, late, reason), context, f"loop/brief/{kind}"
        )
    except Exception as exc:
        log.exception("%s brief failed during dispatch", kind)
        try:
            await _deliver(await _failure_notice(f"{kind} brief", exc), kind, store=False)
        except Exception:
            log.exception("could not notify owner of failed %s brief", kind)
        return

    text = (result.text or "").strip()
    if not text:
        log.warning("%s brief produced no text", kind)
        text = f"The {kind} brief came back empty."

    # Promises he owes are listed in every brief by code, not by the model:
    # a priority ranking once dropped two Example Property Group promises.
    text = _with_owes(text)
    private = await _private(kind)
    if private:
        text = f"{text}\n\n{private}"

    # The evening brief carries the day's health and cost line.
    if kind == "evening":
        try:
            text = f"{text}\n\n{await daily_health_line()}"
        except Exception:
            log.exception("could not append health line to evening brief")
        try:
            text = f"{text}\n{await daily_cost_line()}"
        except Exception:
            log.exception("could not append cost line to evening brief")

    try:
        await _deliver(text, kind)
    except Exception:
        log.exception("could not deliver %s brief", kind)
    else:
        log.info("%s brief delivered (%d chars)", kind, len(text))
        try:
            await set_heartbeat(f"brief/{kind}")
            await _note_brief(True, now, None)
        except Exception:
            log.exception("could not write %s brief heartbeat", kind)


async def run_weekly_review() -> None:
    """Run the CEO's weekly review and deliver it. Never raises."""
    log.info("running weekly review")
    try:
        context = await _weekly_context()
        result = await _dispatch_with_retry("weekly review", _weekly_prompt(), context, "loop/weekly_review")
    except Exception as exc:
        log.exception("weekly review failed during dispatch")
        result = None
        try:
            await _deliver(await _failure_notice("weekly review", exc), "weekly", store=False)
        except Exception:
            log.exception("could not notify owner of failed weekly review")

    if result is not None:
        text = (result.text or "").strip() or "The weekly review came back empty."
        try:
            await _deliver(text, "weekly")
        except Exception:
            log.exception("could not deliver weekly review")
        else:
            log.info("weekly review delivered (%d chars)", len(text))

    # Active forcing loop: prompt the owner to mark each decision due for
    # review Right / Wrong / Mixed. It needs no model, so it runs even when
    # the narrative review failed. Never raises.
    try:
        sent = await send_review_round()
        log.info("decision-review round sent: %d prompt(s)", sent)
    except Exception:
        log.exception("could not run decision-review round")


async def _brief_today(kind: str, tz: ZoneInfo) -> bool:
    """True when a brief of this kind was stored today (local date)."""
    today = datetime.now(tz).date()
    return bool(
        await db.fetchval(
            "select exists (select 1 from briefs where kind = $1 and (created_at at time zone $2)::date = $3)",
            kind,
            settings.timezone,
            today,
        )
    )


# Each brief runs at most once per slot per day in this process: the cron job,
# a misfire after a wake and the catch-up tick all claim it first. A brief
# that failed is not retried by the tick (that would be a model call every
# ten minutes); a restarted worker tries it once more.
_ATTEMPTED: set[tuple[date, str]] = set()
# The catch-up tick leaves a slot to its own cron job for this long.
CATCH_UP_AFTER = timedelta(minutes=5)


def _claim(day: date, kind: str) -> bool:
    if (day, kind) in _ATTEMPTED:
        return False
    _ATTEMPTED.add((day, kind))
    return True


async def catch_up_briefs(now: datetime | None = None) -> str | None:
    """Run today's brief if Atlas missed its slot. Returns the kind run.

    The scheduler keeps jobs in memory and drops a cron run more than an
    hour late, so a worker that starts after 08:00, or a laptop that sleeps
    through it, never fires that day's morning brief. Called at worker
    start, on wake and every ten minutes: if the latest slot that has
    passed today has no stored brief and was not tried, it runs now,
    marked late with the reason. Only the latest one: at 21:00 the evening
    brief replaces a missed morning one."""
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    t = now.timetz().replace(tzinfo=None)
    if t >= settings.brief_evening:
        kind, slot = "evening", settings.brief_evening
    elif t >= settings.brief_morning:
        kind, slot = "morning", settings.brief_morning
    else:
        return None
    slot_at = datetime.combine(now.date(), slot, tzinfo=tz)
    if now < slot_at + CATCH_UP_AFTER or (now.date(), kind) in _ATTEMPTED:
        return None
    try:
        if await _brief_today(kind, tz):
            return None
    except Exception:
        log.exception("catch-up: could not check today's %s brief", kind)
        return None
    if not _claim(now.date(), kind):
        return None
    from core.reliability import late_reason

    await run_brief(kind, late=slot.strftime("%H:%M"), reason=await late_reason(slot_at, now))
    return kind


# Job discovery runs at 05:15 (core/loop/scheduler.py). A worker asleep, down or
# waiting on Postgres then skipped the whole night: 5 of 14 nights after Oct 8.
DISCOVERY_AT = (5, 15)
DISCOVERY_STALE = timedelta(hours=20)
_discovery_running = False


async def catch_up_discovery(now: datetime | None = None) -> bool:
    """Run job discovery, then the apply queue, when today's 05:15 run did not
    happen. True when it ran. Never raises."""
    global _discovery_running
    from core.heartbeats import get_heartbeat

    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    if (now.hour, now.minute) < DISCOVERY_AT or _discovery_running:
        return False
    try:
        last = await get_heartbeat("jobs/discovery")
    except Exception:
        log.exception("catch-up: could not read the discovery heartbeat")
        return False
    if last is not None and now - last.astimezone(tz) < DISCOVERY_STALE:
        return False
    from core.jobs.discovery import scheduled_discover
    from core.jobs.queue import scheduled_build

    _discovery_running = True
    try:
        log.info("catch-up: job discovery last ran %s; running it now", last.isoformat() if last else "never")
        await scheduled_discover()
        await scheduled_build()
    finally:
        _discovery_running = False
    return True


# A catch-up that hangs (a dropped database connection, 2026-10-22 18:06)
# blocks every later tick: APScheduler runs one instance at a time, so the
# evening brief and its catch-up both went missing. Each part gets a limit.
CATCH_UP_BRIEF_LIMIT_S = 20 * 60
CATCH_UP_DISCOVERY_LIMIT_S = 45 * 60
CATCH_UP_CHECK_LIMIT_S = 5 * 60


async def catch_up_tick() -> None:
    """Scheduler entry for the ten-minute catch-up. Never raises."""
    from core.reliability import catch_up_self_check

    for label, run, limit in (
        ("brief", catch_up_briefs, CATCH_UP_BRIEF_LIMIT_S),
        ("slo self-check", catch_up_self_check, CATCH_UP_CHECK_LIMIT_S),
        ("job discovery", catch_up_discovery, CATCH_UP_DISCOVERY_LIMIT_S),
    ):
        try:
            await asyncio.wait_for(run(), timeout=limit)
        except TimeoutError:
            log.error("catch-up: %s gave up after %d min", label, limit // 60)
        except Exception:
            log.exception("catch-up: %s failed", label)


async def catch_up_decision_reviews() -> int:
    """Send the decision-review round now if none went out in the last week.

    The round normally rides the Sunday review; a worker that was down on
    Sunday (or for months) would otherwise leave overdue reviews silent."""
    try:
        recent = await db.fetchval(
            "select exists (select 1 from notifications where kind = 'decision_review' "
            "and created_at > now() - interval '7 days')"
        )
    except Exception:
        log.exception("catch-up: could not check recent decision reviews")
        return 0
    if recent:
        return 0
    return await send_review_round()


async def catch_up() -> None:
    """Worker start-up catch-up: a missed brief, then overdue decision reviews."""
    try:
        await catch_up_briefs()
    except Exception:
        log.exception("catch-up: briefs failed")
    try:
        sent = await catch_up_decision_reviews()
        if sent:
            log.info("catch-up: sent %d overdue decision review(s)", sent)
    except Exception:
        log.exception("catch-up: decision reviews failed")


async def _scheduled(kind: str, now: datetime | None = None) -> None:
    """The cron job for a brief. A run that APScheduler fires late (inside
    its misfire grace, after the laptop woke) says it is late and why."""
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    if not _claim(now.date(), kind):
        log.info("%s brief already ran today; skipping", kind)
        return
    slot = settings.brief_morning if kind == "morning" else settings.brief_evening
    slot_at = datetime.combine(now.date(), slot, tzinfo=tz)
    if now - slot_at > CATCH_UP_AFTER:
        from core.reliability import late_reason

        await run_brief(kind, late=slot.strftime("%H:%M"), reason=await late_reason(slot_at, now))
    else:
        await run_brief(kind)


async def morning_brief() -> None:
    """Scheduler entry point for the morning brief."""
    await _scheduled("morning")


async def evening_brief() -> None:
    """Scheduler entry point for the evening brief."""
    await _scheduled("evening")


async def list_briefs(limit: int = 20, kind: str | None = None) -> list[dict]:
    """Recent briefs for the PWA, newest first."""
    if kind:
        rows = await db.fetch(
            "select * from briefs where kind = $1 order by created_at desc limit $2", kind, limit
        )
    else:
        rows = await db.fetch("select * from briefs order by created_at desc limit $1", limit)
    return [dict(r) for r in rows]


__all__ = [
    "catch_up",
    "catch_up_briefs",
    "catch_up_tick",
    "catch_up_decision_reviews",
    "run_brief",
    "run_weekly_review",
    "morning_brief",
    "evening_brief",
    "list_briefs",
]
