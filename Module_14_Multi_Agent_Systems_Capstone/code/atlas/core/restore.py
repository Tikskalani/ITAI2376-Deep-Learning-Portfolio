"""Atlas -- restore-test a pg_dump backup.

Backups are theatre until one has been restored end to end. This module
restores a `.dump` file (the custom-format archive that
scripts/backup_postgres.py writes) into a freshly-created test database
inside the same atlas-postgres container, then compares per-table row
counts against the live database to confirm the restore is faithful.

Use scripts/restore_postgres.py for the CLI; this module is the
plumbing it calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

# Container name set in docker-compose.yml.
_CONTAINER = "atlas-postgres"


class RestoreError(RuntimeError):
    """Restore failed: dump missing, pg_restore fatal, or schema mismatch."""


def test_db_name(now: datetime | None = None) -> str:
    """A timestamped test-db name that does not collide with the live db
    or with concurrent runs."""
    when = now or datetime.now(UTC)
    return f"atlas_restore_test_{when.strftime('%Y%m%d_%H%M%S')}"


async def _run(
    *cmd: str, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    """Run a shell command, capturing stdout and stderr."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    return (proc.returncode or 0), (out or b""), (err or b"")


async def row_counts(db: str) -> dict[str, int]:
    """Count rows in every public table of `db`. Uses psql inside the
    atlas-postgres container so we do not need a Python driver pointed
    at the test database."""
    code, out, err = await _run(
        "docker",
        "exec",
        _CONTAINER,
        "psql",
        "-U",
        settings.pg_user,
        "-d",
        db,
        "-At",
        "-c",
        (
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by table_name"
        ),
    )
    if code != 0:
        raise RestoreError(
            f"could not list tables in {db}: "
            f"{err.decode(errors='replace').strip()[:300]}"
        )
    tables = [t for t in out.decode().splitlines() if t.strip()]

    counts: dict[str, int] = {}
    for table in tables:
        c, o, e = await _run(
            "docker",
            "exec",
            _CONTAINER,
            "psql",
            "-U",
            settings.pg_user,
            "-d",
            db,
            "-At",
            "-c",
            f'select count(*) from "{table}"',
        )
        if c != 0:
            raise RestoreError(
                f"count failed for {db}.{table}: "
                f"{e.decode(errors='replace').strip()[:300]}"
            )
        counts[table] = int(o.decode().strip() or "0")
    return counts


async def restore_to_test_db(
    dump_path: Path, test_db: str | None = None
) -> str:
    """Create a fresh test database and restore the dump into it. Returns
    the test database name.
    """
    if not dump_path.is_file():  # noqa: ASYNC240
        raise RestoreError(f"dump file not found: {dump_path}")

    name = test_db or test_db_name()

    # Drop any existing test db with this name (idempotent).
    await _run(
        "docker",
        "exec",
        _CONTAINER,
        "dropdb",
        "-U",
        settings.pg_user,
        "--if-exists",
        name,
    )

    code, _, err = await _run(
        "docker",
        "exec",
        _CONTAINER,
        "createdb",
        "-U",
        settings.pg_user,
        name,
    )
    if code != 0:
        raise RestoreError(
            f"createdb {name} failed: "
            f"{err.decode(errors='replace').strip()[:300]}"
        )

    log.info("running pg_restore -> %s", name)
    dump_bytes = dump_path.read_bytes()  # noqa: ASYNC240
    code, _, err = await _run(
        "docker",
        "exec",
        "-i",
        _CONTAINER,
        "pg_restore",
        "-U",
        settings.pg_user,
        "-d",
        name,
        "--no-owner",
        "--no-acl",
        stdin=dump_bytes,
    )
    # pg_restore exits non-zero for harmless warnings (extension already
    # installed at the server level, comments on objects we did not own).
    # Treat only lines containing FATAL or could-not-... as breaking.
    err_text = err.decode(errors="replace")
    if code != 0 and ("FATAL" in err_text or "could not" in err_text):
        raise RestoreError(
            f"pg_restore failed: {err_text.strip()[:500]}"
        )
    return name


async def drop_test_db(name: str) -> None:
    """Drop a test database. Idempotent; safe to call after a failed run."""
    await _run(
        "docker",
        "exec",
        _CONTAINER,
        "dropdb",
        "-U",
        settings.pg_user,
        "--if-exists",
        name,
    )


def compare_counts(
    source: dict[str, int], restored: dict[str, int]
) -> list[dict[str, Any]]:
    """Per-table comparison report. Status is one of: match, drift,
    missing_after_restore, only_in_restore."""
    all_tables = sorted(set(source) | set(restored))
    report: list[dict[str, Any]] = []
    for t in all_tables:
        s = source.get(t)
        r = restored.get(t)
        if s is None:
            status = "only_in_restore"
        elif r is None:
            status = "missing_after_restore"
        elif s == r:
            status = "match"
        else:
            status = "drift"
        report.append(
            {"table": t, "source": s, "restored": r, "status": status}
        )
    return report


__all__ = [
    "RestoreError",
    "test_db_name",
    "row_counts",
    "restore_to_test_db",
    "drop_test_db",
    "compare_counts",
]
