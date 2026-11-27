"""Atlas -- the in-process scheduler.

An APScheduler AsyncIOScheduler that runs on the worker's event loop
(scripts/run_worker.py). Exactly one worker runs at a time; two would mean
every brief and loop tick twice.

Jobs: the morning and evening briefs, the executive loop (triggers plus
the senses cycle), the Sunday weekly review and memory hygiene, the
nightly backup, health alerts, the discovery scan and learning digest,
the weekly learnings review, approval expiry, and the email fallback for
unread notifications. Prospective triggers are not scheduled here; the
executive loop polls and fires them on each tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.backup import daily_backup_job
from core.config import settings
from core.jobs.discovery import scheduled_discover
from core.jobs.followups import scheduled_daily as jobs_daily
from core.jobs.mail import scheduled_scan as jobs_mail_scan
from core.jobs.queue import scheduled_build as jobs_queue_build
from core.jobs.submitter import scheduled_run as jobs_submit_run
from core.kb import scheduled_batch as kb_import
from core.loop.briefs import catch_up_tick, evening_brief, morning_brief, run_weekly_review
from core.loop.discovery_scan import run_discovery_scan
from core.loop.envwatch import tick as env_watch
from core.loop.executive import run_executive_cycle
from core.loop.hygiene import run_hygiene
from core.loop.learning_digest import run_learning_digest, weekly_learnings_review
from core.loop.webwatch import tick as web_watch
from core.mail_learning import scheduled_learn, scheduled_refresh
from core.memory.consolidation import run_consolidation
from core.news import scheduled_brief as news_brief
from core.night import scheduled_propose as night_propose
from core.night import scheduled_report as night_report
from core.night import scheduled_run as night_run
from core.recurring import scheduled_materialize as recurring_tasks
from core.reflection import scheduled_reflect as playbook_reflection
from core.reliability import scheduled_self_check as slo_self_check
from core.runtime.approval import expire_overdue_approvals
from core.runtime.late import sweep as finish_late_approvals
from core.surfaces.notify import email_unread_notifications
from core.workflow_engine import tick as workflow_tick

log = logging.getLogger(__name__)

# Weekly jobs run Sunday. The review lands late afternoon; hygiene runs
# early so its findings are fresh for the review. Hardcoded for now; they
# can move to settings if Trilok wants to tune them.
_WEEKLY_REVIEW_HOUR = 17
_HYGIENE_HOUR = 6
# Nightly discovery scan runs in the evening, before the digest lands.
_DISCOVERY_SCAN_HOUR = 21
_LEARNINGS_REVIEW_HOUR = 18  # Sunday, after the weekly review
_CONSOLIDATION_HOUR = 2  # nightly, before the 03:00 backup


def build_scheduler() -> AsyncIOScheduler:
    """Build the scheduler with the briefs, executive loop, and weekly jobs.

    The caller starts it (scheduler.start()) once the event loop is
    running and shuts it down on exit. misfire_grace_time lets a job
    still fire if the bot was briefly down at its scheduled moment.
    """
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        morning_brief,
        CronTrigger(
            hour=settings.brief_morning.hour,
            minute=settings.brief_morning.minute,
            timezone=tz,
        ),
        id="morning_brief",
        name="Morning brief",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        evening_brief,
        CronTrigger(
            hour=settings.brief_evening.hour,
            minute=settings.brief_evening.minute,
            timezone=tz,
        ),
        id="evening_brief",
        name="Evening brief",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # A brief whose slot passed while the laptop slept or the worker was down
    # runs here, once, marked late with the reason (the cron job above drops a
    # run more than an hour late, and catch-up at start only covered restarts).
    scheduler.add_job(
        catch_up_tick,
        IntervalTrigger(minutes=10, timezone=tz),
        id="brief_catch_up_tick",
        name="Catch up a missed brief",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    # The reliability SLO: did each brief land within 15 minutes, did the loop
    # keep ticking. On wake before 06:00 it judges the day before.
    scheduler.add_job(
        slo_self_check,
        CronTrigger(hour=23, minute=50, timezone=tz),
        id="slo_self_check",
        name="Nightly reliability self-check",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_executive_cycle,
        IntervalTrigger(minutes=settings.loop_interval_min, timezone=tz),
        id="executive_loop",
        name="Executive loop",
        replace_existing=True,
        # The laptop sleeps. A tick that wakes up to twenty minutes late still
        # runs (coalesced into one), instead of being skipped until the next
        # half hour; two sleeps in a row used to leave the loop silent for an hour.
        misfire_grace_time=1200,
        coalesce=True,
        # A fresh worker ticks a minute after it starts, not a full interval later:
        # a restart used to leave the loop silent for 30 minutes while health
        # showed the previous process's tick.
        next_run_time=datetime.now(tz) + timedelta(seconds=60),
    )
    scheduler.add_job(
        run_weekly_review,
        CronTrigger(
            day_of_week="sun", hour=_WEEKLY_REVIEW_HOUR, minute=0, timezone=tz
        ),
        id="weekly_review",
        name="Weekly review",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_hygiene,
        CronTrigger(
            day_of_week="sun", hour=_HYGIENE_HOUR, minute=0, timezone=tz
        ),
        id="memory_hygiene",
        name="Memory hygiene",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    log.info(
        "scheduler built: morning %02d:%02d, evening %02d:%02d, "
        "executive loop every %dm, weekly review Sun %02d:00, "
        "hygiene Sun %02d:00 (%s)",
        settings.brief_morning.hour,
        settings.brief_morning.minute,
        settings.brief_evening.hour,
        settings.brief_evening.minute,
        settings.loop_interval_min,
        _WEEKLY_REVIEW_HOUR,
        _HYGIENE_HOUR,
        settings.timezone,
    )
    scheduler.add_job(
        daily_backup_job,
        CronTrigger(hour=settings.backup_hour, minute=0, timezone=tz),
        id="daily_backup",
        name="Daily pg_dump backup",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # No health_alert job here: a stalled worker cannot report its own stall.
    # The web process runs core.loop.health.watch_forever instead.

    scheduler.add_job(
        run_discovery_scan,
        CronTrigger(hour=_DISCOVERY_SCAN_HOUR, minute=0, timezone=tz),
        id="discovery_scan",
        name="Nightly discovery scan",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        run_learning_digest,
        CronTrigger(hour=_DISCOVERY_SCAN_HOUR, minute=30, timezone=tz),
        id="learning_digest",
        name="Nightly learning digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        weekly_learnings_review,
        CronTrigger(day_of_week="sun", hour=_LEARNINGS_REVIEW_HOUR, minute=0, timezone=tz),
        id="weekly_learnings_review",
        name="Weekly learnings review",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Approval cards past their TTL expire here (the requester may be long
    # gone); unread notifications older than an hour get the email fallback.
    scheduler.add_job(
        expire_overdue_approvals,
        IntervalTrigger(minutes=5, timezone=tz),
        id="expire_approvals",
        name="Expire overdue approvals",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    # A yes that arrives after the agent stopped waiting is finished by the
    # web process on the spot; this sweep catches anything it did not get to.
    scheduler.add_job(
        finish_late_approvals,
        IntervalTrigger(minutes=2, timezone=tz),
        id="late_approvals",
        name="Finish late-approved actions",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,
    )
    # The knowledge base from Claude: a dropped export is read twenty
    # conversations a minute until it is done or hits its cap.
    scheduler.add_job(
        kb_import,
        IntervalTrigger(minutes=1, timezone=tz),
        id="kb_import",
        name="Read the claude.ai export",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,
    )
    # A token connected from the dashboard lands in .env; the worker reloads
    # its settings when the file changes, so no restart stands between a
    # connection and the next scan.
    scheduler.add_job(
        env_watch,
        IntervalTrigger(minutes=1, timezone=tz),
        id="env_reload",
        name="Reload settings when .env changes",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,
    )
    # The other half of the watchdog: the worker restarts the web app after
    # three missed health checks (the web process watches the worker).
    if settings.worker_watches_web:
        scheduler.add_job(
            web_watch,
            IntervalTrigger(minutes=2, timezone=tz),
            id="web_watch",
            name="Watch the web app",
            replace_existing=True,
            misfire_grace_time=120,
            coalesce=True,
            max_instances=1,
        )
    scheduler.add_job(
        email_unread_notifications,
        IntervalTrigger(minutes=15, timezone=tz),
        id="email_unread_notifications",
        name="Email fallback for unread notifications",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
    )

    # Nightly consolidation: dedupe and decay facts, expire profile
    # constraints, regenerate the per-project context packs.
    scheduler.add_job(
        run_consolidation,
        CronTrigger(hour=_CONSOLIDATION_HOUR, minute=30, timezone=tz),
        id="memory_consolidation",
        name="Nightly memory consolidation",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Mail learning: who matters, what is noise and what is open, from his
    # own Gmail. Daily before the morning brief (open loops; a full pass if
    # nothing is learned yet), weekly full pass on Sunday morning. Both are
    # no-ops until Gmail is connected.
    scheduler.add_job(
        scheduled_refresh,
        CronTrigger(hour=6, minute=40, timezone=tz),
        id="mail_open_loops",
        name="Daily open loops from Gmail",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # The agents write down what worked: the day's worthwhile runs read once
    # a night, a procedure proposed on the Learn tab when there is one.
    scheduler.add_job(
        playbook_reflection,
        CronTrigger(hour=3, minute=10, timezone=tz),
        id="playbook_reflection",
        name="Self-written playbook proposals",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )
    # Recurring tasks (the mortgage on the 1st, the review every Friday) are
    # written onto the Planner three weeks ahead, once each, just after midnight.
    scheduler.add_job(
        recurring_tasks,
        CronTrigger(hour=0, minute=10, timezone=tz),
        id="recurring_tasks",
        name="Write recurring tasks onto the Planner",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )
    # The morning AI brief: the trusted feeds swept, the handful that matter
    # picked and pushed, before the morning brief proper.
    scheduler.add_job(
        news_brief,
        CronTrigger(hour=6, minute=45, timezone=tz),
        id="news_brief",
        name="Morning AI brief",
        replace_existing=True,
        misfire_grace_time=3 * 3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        scheduled_learn,
        CronTrigger(day_of_week="sun", hour=5, minute=0, timezone=tz),
        id="mail_learning",
        name="Weekly mail learning",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )

    # Durable workflow runs: anything waiting on an approval, a timer or a
    # retry moves forward here.
    scheduler.add_job(
        workflow_tick,
        IntervalTrigger(minutes=2, timezone=tz),
        id="workflow_tick",
        name="Advance workflow runs",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,
    )

    # The job search (R9b): Gmail filing every three hours (rules first,
    # free), board discovery before dawn, the apply queue drafted from it,
    # follow-ups before the morning brief.
    scheduler.add_job(
        jobs_mail_scan,
        IntervalTrigger(hours=3, timezone=tz),
        id="jobs_mail",
        name="File job-application mail",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        scheduled_discover,
        CronTrigger(hour=5, minute=15, timezone=tz),
        id="jobs_discovery",
        name="Find roles on watched job boards",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        jobs_queue_build,
        CronTrigger(hour=7, minute=0, timezone=tz),
        id="jobs_queue",
        name="Draft today's apply queue",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )
    # Approved applications go in (dry run until config/jobs.yaml says not).
    scheduler.add_job(
        jobs_submit_run,
        IntervalTrigger(minutes=20),
        id="jobs_submit",
        name="Submit approved applications",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # The night shift: propose in the evening, run late, report in the morning.
    scheduler.add_job(
        night_propose,
        CronTrigger(hour=settings.night_propose_hour, minute=settings.night_propose_minute, timezone=tz),
        id="night_propose",
        name="Propose tonight's tasks",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        night_run,
        CronTrigger(hour=settings.night_run_hour, minute=settings.night_run_minute, timezone=tz),
        id="night_run",
        name="Run the night shift",
        replace_existing=True,
        misfire_grace_time=3 * 3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        night_report,
        CronTrigger(hour=settings.night_report_hour, minute=settings.night_report_minute, timezone=tz),
        id="night_report",
        name="Night shift report",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        jobs_daily,
        CronTrigger(hour=7, minute=20, timezone=tz),
        id="jobs_daily",
        name="Job follow-ups and interview prep",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )

    return scheduler


__all__ = ["build_scheduler"]
