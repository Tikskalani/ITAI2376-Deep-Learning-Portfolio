"""Unit tests for the Postgres backup layer.

asyncio.create_subprocess_exec is mocked, so no docker, no pg_dump, no
real Postgres. File system writes use tmp_path so each test is isolated.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backup import (
    BackupError,
    _backup_filename,
    daily_backup_job,
    prune_old_backups,
    run_backup,
)


def _fake_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def test_backup_filename_format():
    name = _backup_filename(datetime(2026, 5, 26, 3, 0, 0))
    assert name == "atlas-2026-05-26-030000.dump"


async def test_run_backup_writes_file(tmp_path: Path):
    fake_dump = b"\x00\x01PGDMP fake dump content"
    proc = _fake_proc(0, stdout=fake_dump)
    with patch("core.backup.settings.backup_dir", tmp_path), patch(
        "core.backup.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        out = await run_backup(datetime(2026, 5, 26, 12, 34, 56, tzinfo=UTC))

    assert out.exists()
    assert out.read_bytes() == fake_dump  # noqa: ASYNC240
    assert out.name == "atlas-2026-05-26-123456.dump"
    assert out.parent == tmp_path


async def test_run_backup_raises_on_nonzero_exit(tmp_path: Path):
    proc = _fake_proc(1, stderr=b"connection refused")
    with patch("core.backup.settings.backup_dir", tmp_path), patch(
        "core.backup.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(BackupError) as exc:
            await run_backup()
    assert "connection refused" in str(exc.value)


async def test_run_backup_raises_on_empty_output(tmp_path: Path):
    proc = _fake_proc(0, stdout=b"")
    with patch("core.backup.settings.backup_dir", tmp_path), patch(
        "core.backup.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(BackupError) as exc:
            await run_backup()
    assert "empty file" in str(exc.value)
    # No partial file left behind.
    assert list(tmp_path.glob("atlas-*.dump")) == []  # noqa: ASYNC240


def test_prune_old_backups_removes_only_old_files(tmp_path: Path):
    # Three files: very old, recent, brand new.
    old = tmp_path / "atlas-2024-01-01-000000.dump"
    middle = tmp_path / "atlas-2026-05-20-000000.dump"
    fresh = tmp_path / "atlas-2026-05-26-000000.dump"
    for p in (old, middle, fresh):
        p.write_bytes(b"x")

    # Set mtimes: old = 60 days ago, middle = 5 days ago, fresh = now
    now = time.time()
    os.utime(old, (now - 60 * 86400, now - 60 * 86400))
    os.utime(middle, (now - 5 * 86400, now - 5 * 86400))
    os.utime(fresh, (now, now))

    with patch("core.backup.settings.backup_dir", tmp_path):
        removed = prune_old_backups(retention_days=14)

    assert old in removed
    assert middle not in removed  # within window
    assert fresh not in removed  # newest always kept
    assert not old.exists()
    assert middle.exists()
    assert fresh.exists()


def test_prune_keeps_newest_even_if_outside_window(tmp_path: Path):
    # Only one file, older than retention: newest-kept rule wins.
    only = tmp_path / "atlas-2024-01-01-000000.dump"
    only.write_bytes(b"x")
    os.utime(only, (time.time() - 365 * 86400, time.time() - 365 * 86400))

    with patch("core.backup.settings.backup_dir", tmp_path):
        removed = prune_old_backups(retention_days=14)
    assert removed == []
    assert only.exists()


def test_prune_handles_missing_directory(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    with patch("core.backup.settings.backup_dir", missing):
        assert prune_old_backups(retention_days=14) == []


async def test_daily_backup_job_swallows_errors(tmp_path: Path):
    """daily_backup_job must never propagate exceptions or the scheduler dies."""
    with patch("core.backup.settings.backup_dir", tmp_path), patch(
        "core.backup.run_backup",
        new=AsyncMock(side_effect=BackupError("docker not running")),
    ), patch("core.backup.prune_old_backups", return_value=[]):
        # Must not raise.
        await daily_backup_job()


async def test_daily_backup_job_calls_run_then_prune(tmp_path: Path):
    fake_path = tmp_path / "atlas-test.dump"
    fake_path.write_bytes(b"abc")
    with patch(
        "core.backup.run_backup", new=AsyncMock(return_value=fake_path)
    ) as rb, patch(
        "core.backup.prune_old_backups", return_value=[]
    ) as pr:
        await daily_backup_job()
    rb.assert_awaited_once()
    pr.assert_called_once()
