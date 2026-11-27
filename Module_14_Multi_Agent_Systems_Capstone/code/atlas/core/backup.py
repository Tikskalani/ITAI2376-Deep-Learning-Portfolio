"""Atlas -- Postgres backup.

A daily pg_dump of the Atlas database, run inside the existing
atlas-postgres Docker container, written to settings.backup_dir as a
timestamped custom-format archive. Old backups are rotated out by age.

Why custom format (-F c):
  * single file per backup, smaller than plain SQL
  * pg_restore can restore selective tables if ever needed
  * compresses well; pg_dump's -Z 9 brings the file size down further

The job is wired into the scheduler so it fires daily without further
action. scripts/backup_postgres.py runs the same job from the shell for
ad-hoc backups or to seed a known good state before risky changes.

run_backup raises BackupError on a non-zero pg_dump exit; the wrapper
daily_backup_job catches and logs so a transient failure does not crash
the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

# Container name set in docker-compose.yml.
_CONTAINER_NAME = "atlas-postgres"


class BackupError(RuntimeError):
    """pg_dump returned non-zero, or the backup file was unreadable."""


def _backup_filename(when: datetime) -> str:
    """Stable filename per run: atlas-YYYY-MM-DD-HHMMSS.dump."""
    return f"atlas-{when.strftime('%Y-%m-%d-%H%M%S')}.dump"


async def run_backup(now: datetime | None = None) -> Path:
    """Run pg_dump and write the dump to settings.backup_dir.

    Returns the path to the written file. Raises BackupError on failure.
    """
    when = now or datetime.now(UTC)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.backup_dir / _backup_filename(when)

    # pg_dump runs INSIDE the container; we capture stdout and write it
    # to the host filesystem. -F c is the custom binary format; -Z 9 is
    # max gzip compression.
    cmd = [
        "docker",
        "exec",
        _CONTAINER_NAME,
        "pg_dump",
        "-U",
        settings.pg_user,
        "-d",
        settings.pg_db,
        "-F",
        "c",
        "-Z",
        "9",
    ]

    log.info("running pg_dump -> %s", out_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise BackupError(
            f"pg_dump exited {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()[:500]}"
        )

    out_path.write_bytes(stdout)
    if out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise BackupError("pg_dump produced an empty file")

    return out_path


def prune_old_backups(retention_days: int | None = None) -> list[Path]:
    """Delete .dump files older than retention_days. Returns paths removed.

    Files are kept if their mtime is within the retention window; the
    most recent file is always kept regardless of age, so a long
    downtime cannot leave the directory empty.
    """
    keep_days = (
        retention_days
        if retention_days is not None
        else settings.backup_retention_days
    )
    if not settings.backup_dir.exists():
        return []

    files = sorted(
        settings.backup_dir.glob("atlas-*.dump"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return []

    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    removed: list[Path] = []
    # Keep the newest file always (files[-1]).
    for path in files[:-1]:
        if path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    return removed


def mirror_capabilities(src: Path | None = None, dest: Path | None = None) -> int:
    """Copy capability evidence (frames, under data/capabilities/) into the
    backup folder, so a database restored from a dump does not point at
    evidence that is gone. Copies files that are new or changed; never
    deletes from the backup. The downloaded videos themselves are not kept
    (they can be fetched again; their frames and transcript are the
    evidence). Returns how many files were copied."""
    import shutil

    src = src or settings.capabilities_dir
    dest = dest or settings.backup_dir / "capabilities"
    if not src.is_dir():
        return 0
    copied = 0
    for f in src.rglob("*"):
        if not f.is_file() or f.name.startswith("video."):
            continue
        target = dest / f.relative_to(src)
        if target.exists() and target.stat().st_size == f.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1
    return copied


async def daily_backup_job() -> None:
    """The scheduler's entry point: run a backup, prune old ones, log.

    Never raises; a failure is logged so the scheduler keeps running.
    """
    try:
        path = await run_backup()
        size_mb = path.stat().st_size / (1024 * 1024)
        log.info(
            "backup complete: %s (%.2f MB)",
            path.name,
            size_mb,
        )
    except Exception:
        log.exception("backup failed")
        return

    try:
        copied = mirror_capabilities()
        if copied:
            log.info("backup: copied %d capability evidence file(s)", copied)
    except Exception:
        log.exception("capability evidence backup failed")

    try:
        removed = prune_old_backups()
        if removed:
            log.info(
                "pruned %d old backup(s): %s",
                len(removed),
                ", ".join(p.name for p in removed),
            )
    except Exception:
        log.exception("backup prune failed")

    # Chain the offsite sync. Imported inline so backup.py stays usable
    # if backup_offsite is missing during tests or partial rollouts.
    try:
        from core.backup_offsite import offsite_sync_job
        await offsite_sync_job()
    except Exception:
        log.exception("offsite sync failed")


__all__ = [
    "BackupError",
    "run_backup",
    "prune_old_backups",
    "daily_backup_job",
]
