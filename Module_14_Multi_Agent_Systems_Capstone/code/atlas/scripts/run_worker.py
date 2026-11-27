"""Atlas worker entrypoint: the scheduler and nothing else.

Runs the briefs, the executive loop (triggers and the senses cycle),
hygiene, backups, health alerts, discovery, approval expiry and the email
fallback. The web surface (scripts/run_web.py) is a separate process;
both register the same agents. Exactly one worker may run at a time.

Initializes structured logging into logs/, warms the asyncpg pool,
registers agents with the dispatcher, starts the scheduler, and runs
until SIGINT/SIGTERM (or Ctrl+C from NSSM on Windows). On shutdown the
scheduler stops, in-flight jobs are given a moment, and the DB pool
closes.

Run from the repo root:

    python -m scripts.run_worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler

from apscheduler.triggers.date import DateTrigger

from core import db, liveness, reliability
from core.config import settings
from core.heartbeats import set_heartbeat
from core.loop.briefs import catch_up, catch_up_briefs
from core.loop.scheduler import build_scheduler
from core.runtime.register_all import register_all
from core.surfaces.notify import email_configured, push_configured

log = logging.getLogger("atlas.run_worker")


def _setup_logging() -> None:
    """Bootstrap logging: stderr (the --log-file when the service runs it) +
    rotating file under settings.log_dir."""
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Wipe any default handlers so re-runs don't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    fileh = RotatingFileHandler(
        settings.log_dir / "atlas-worker.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    from core.logredact import install as install_redaction

    install_redaction()   # no secret reaches stderr or the log file

    if root.level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.INFO)


async def _main_async() -> None:
    _setup_logging()
    log.info("Atlas worker starting (env=%s, tz=%s)", settings.env, settings.timezone)
    if not push_configured():
        log.warning("push is not configured (ATLAS_VAPID_*); briefs will wait in the inbox")
    if not email_configured():
        log.info("email fallback is off (ATLAS_SMTP_HOST / ATLAS_NOTIFY_EMAIL blank)")

    # After a boot or a wake Docker can take minutes to bring Postgres back;
    # crashing here made NSSM restart the worker every two minutes for an hour.
    await reliability.wait_for_db()
    log.info("postgres pool ready")

    roster = register_all()
    log.info("agents registered with the dispatcher: %s", ", ".join(sorted(roster)))

    scheduler = build_scheduler()
    scheduler.start()
    log.info("scheduler started with %d job(s)", len(scheduler.get_jobs()))
    # Tells the web watchdog and /api/health a fresh worker is up, so its
    # first interval reads as "starting", not "stalled".
    try:
        await set_heartbeat("worker/started", {"pid": os.getpid()})
    except Exception:
        log.exception("could not write worker/started heartbeat")
    try:
        await reliability.record_start()
    except Exception:
        log.exception("could not record the worker start")

    resume_tasks: set[asyncio.Task] = set()

    async def _on_resume(asleep_from: datetime, asleep_to: datetime) -> None:
        # The machine slept: note it (late briefs say so), then catch up.
        # A brief can take minutes; run it beside the pulse, never inside it.
        await reliability.record_sleep(asleep_from, asleep_to)
        resume_tasks.add(task := asyncio.get_running_loop().create_task(catch_up_briefs()))
        task.add_done_callback(resume_tasks.discard)

    pulse_task = liveness.start(on_resume=_on_resume)
    # A worker that starts after a brief's slot would skip it for the day
    # (jobs live in memory), and overdue decision reviews wait for Sunday.
    # Give the network two minutes, then catch up on both.
    scheduler.add_job(
        catch_up,
        DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=2)),
        id="brief_catch_up",
        name="Catch up missed briefs and reviews",
        replace_existing=True,
    )

    stop_event = asyncio.Event()

    def _trigger_stop(signame: str) -> None:
        log.info("received %s, shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _trigger_stop, sig.name)
            except NotImplementedError:
                pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("interrupted, shutting down")

    pulse_task.cancel()
    log.info("stopping scheduler")
    scheduler.shutdown(wait=False)
    await asyncio.sleep(0.5)
    log.info("closing postgres pool")
    await db.close_pool()
    log.info("shutdown complete")


def main() -> None:
    """Sync entrypoint. Handles KeyboardInterrupt on Windows where signal
    handlers aren't available (NSSM sends Ctrl+C on stop)."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--log-file", default="",
                        help="write all output here instead of the inherited console (the Windows service uses logs\\worker.log)")
    args = parser.parse_args()
    if args.log_file:
        from core.service_io import use_log_file

        use_log_file(args.log_file)
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        logging.getLogger("atlas.run_worker").info("KeyboardInterrupt at top level, exiting")


if __name__ == "__main__":
    main()
