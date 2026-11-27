"""Atlas -- offsite backup.

Daily pg_dump runs locally; this module replicates the backup directory to
an offsite destination so a single-disk failure does not erase the audit
trail. The default backend is rclone, a one-binary tool that talks to S3,
Backblaze B2, Cloudflare R2, Google Drive, OneDrive, Dropbox, SFTP, WebDAV
and roughly seventy other providers. Trilok picks the provider; Atlas
calls the binary.

Why rclone and not boto3:
  * one install line covers every plausible destination
  * credentials live in rclone's encrypted config, never in Atlas
  * if Trilok switches from B2 to S3 next year, code does not change
  * tests can mock subprocess without pretending to know an API surface

Backends:
  none   -- the default; logs a one-line reminder and returns no-op stats
  rclone -- shells out to `rclone copy` (and `rclone delete --min-age` for
            remote retention pruning, if configured)

The job is chained off the local daily_backup_job so an offsite failure
never blocks a successful local dump from being recorded.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)


# How long subprocess calls are allowed to run before Atlas gives up.
# rclone is incremental, so the second day onward should be fast; the
# first day's upload of an entire backup directory may take a while on
# a slow link. Five minutes is generous without being unbounded.
_RCLONE_TIMEOUT_S = 300


def _no_op_stats(reason: str) -> dict[str, Any]:
    """Return a stats dict for the no-op path (backend=none or skipped)."""
    return {
        "backend": "none",
        "uploaded": 0,
        "pruned": 0,
        "ok": True,
        "reason": reason,
    }


async def _run(cmd: list[str], *, timeout: float = _RCLONE_TIMEOUT_S) -> tuple[int, str, str]:  # noqa: ASYNC109
    """Run a subprocess to completion and return (code, stdout, stderr).

    Times out cleanly; never raises CancelledError on timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return (124, "", f"timeout after {timeout}s")
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def _rclone_sync(
    rclone: str, local_dir: str, remote: str
) -> dict[str, Any]:
    """Upload local_dir to remote via `rclone copy` (incremental)."""
    cmd = [rclone, "copy", local_dir, remote, "--no-traverse"]
    code, stdout, stderr = await _run(cmd)
    return {
        "code": code,
        "stdout": stdout.strip()[-400:],
        "stderr": stderr.strip()[-400:],
    }


async def _rclone_prune(
    rclone: str, remote: str, retention_days: int
) -> dict[str, Any]:
    """Delete database dumps on `remote` older than retention_days. No-op if
    days<=0. Only dumps: the capability evidence beside them (frames, under
    capabilities/) is what restored records point at, and is never pruned."""
    if retention_days <= 0:
        return {"code": 0, "stdout": "", "stderr": "skipped"}
    cmd = [
        rclone,
        "delete",
        remote,
        "--min-age",
        f"{retention_days}d",
        "--include",
        "atlas-*.dump",
    ]
    code, stdout, stderr = await _run(cmd)
    return {
        "code": code,
        "stdout": stdout.strip()[-400:],
        "stderr": stderr.strip()[-400:],
    }


async def offsite_sync() -> dict[str, Any]:
    """Run the configured offsite sync. Never raises.

    Reads settings:
      offsite_backend     "rclone" or "none"
      offsite_remote      e.g. "b2:atlas-backups/db"
      offsite_retention_days  remote retention; 0 disables remote prune
      offsite_rclone_path path to the rclone binary; default "rclone"

    Returns a stats dict the daily job can log and the daily brief can
    quote. Even on failure the dict has ok=False and a reason; nothing
    here ever raises.
    """
    backend = (settings.offsite_backend or "none").lower()
    if backend == "none":
        return _no_op_stats("offsite_backend not configured")

    if backend != "rclone":
        log.warning("unknown offsite_backend %r; skipping", backend)
        return {
            "backend": backend,
            "uploaded": 0,
            "pruned": 0,
            "ok": False,
            "reason": f"unknown backend {backend!r}",
        }

    remote = (settings.offsite_remote or "").strip()
    if not remote:
        return _no_op_stats("offsite_remote not set; check .env")

    rclone = settings.offsite_rclone_path or "rclone"
    if not shutil.which(rclone):
        return {
            "backend": "rclone",
            "uploaded": 0,
            "pruned": 0,
            "ok": False,
            "reason": f"rclone binary not found at {rclone!r}",
        }

    local_dir = str(settings.backup_dir)
    sync = await _rclone_sync(rclone, local_dir, remote)
    if sync["code"] != 0:
        log.warning(
            "offsite sync failed: rclone code=%s stderr=%s",
            sync["code"],
            sync["stderr"],
        )
        return {
            "backend": "rclone",
            "uploaded": 0,
            "pruned": 0,
            "ok": False,
            "reason": f"rclone copy exit {sync['code']}: {sync['stderr'][:200]}",
            "sync": sync,
        }

    prune = await _rclone_prune(
        rclone, remote, settings.offsite_retention_days
    )
    return {
        "backend": "rclone",
        "uploaded": 1,
        "pruned": 1 if prune["code"] == 0 and prune["stderr"] != "skipped" else 0,
        "ok": True,
        "remote": remote,
        "sync": sync,
        "prune": prune,
    }


async def offsite_sync_job() -> None:
    """Scheduler / daily_backup_job entry point. Never raises."""
    try:
        stats = await offsite_sync()
    except Exception:
        log.exception("offsite sync raised unexpectedly")
        return
    if stats["ok"]:
        log.info("offsite sync ok: %s", stats.get("remote") or stats.get("reason"))
    else:
        log.warning("offsite sync skipped/failed: %s", stats.get("reason"))


__all__ = ["offsite_sync", "offsite_sync_job"]
