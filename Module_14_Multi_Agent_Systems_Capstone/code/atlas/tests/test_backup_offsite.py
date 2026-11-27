"""Unit tests for core/backup_offsite.py.

Subprocess and config are mocked; no rclone binary or remote is touched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from core import backup_offsite as off


def _patch_settings(**over):
    """Patch the relevant settings attrs for one test."""
    targets = {
        "offsite_backend": "none",
        "offsite_remote": "",
        "offsite_retention_days": 30,
        "offsite_rclone_path": "rclone",
        "backup_dir": "/tmp/atlas-backups-test",
    }
    targets.update(over)
    patches = [
        patch.object(off.settings, name, value)
        for name, value in targets.items()
    ]
    return patches


async def test_offsite_none_returns_noop():
    with patches_context(_patch_settings(offsite_backend="none")):
        result = await off.offsite_sync()
    assert result["backend"] == "none"
    assert result["ok"] is True
    assert result["uploaded"] == 0
    assert "not configured" in result["reason"]


async def test_offsite_rclone_missing_remote_no_ops():
    with patches_context(
        _patch_settings(offsite_backend="rclone", offsite_remote="")
    ):
        result = await off.offsite_sync()
    assert result["ok"] is True
    assert "offsite_remote not set" in result["reason"]


async def test_offsite_rclone_binary_not_found_reports():
    with (
        patches_context(
            _patch_settings(
                offsite_backend="rclone",
                offsite_remote="b2:atlas/db",
                offsite_rclone_path="/nonexistent/rclone",
            )
        ),
        patch("core.backup_offsite.shutil.which", return_value=None),
    ):
        result = await off.offsite_sync()
    assert result["ok"] is False
    assert "binary not found" in result["reason"]


async def test_offsite_rclone_success_pruning_enabled():
    """Both copy and delete succeed."""
    run = AsyncMock(side_effect=[(0, "", ""), (0, "", "")])
    with (
        patches_context(
            _patch_settings(
                offsite_backend="rclone",
                offsite_remote="b2:atlas/db",
                offsite_retention_days=30,
            )
        ),
        patch("core.backup_offsite.shutil.which", return_value="/usr/bin/rclone"),
        patch("core.backup_offsite._run", new=run),
    ):
        result = await off.offsite_sync()
    assert result["ok"] is True
    assert result["uploaded"] == 1
    assert result["pruned"] == 1
    # Two subprocess calls: copy, then delete.
    assert run.await_count == 2
    copy_cmd = run.await_args_list[0].args[0]
    delete_cmd = run.await_args_list[1].args[0]
    assert "copy" in copy_cmd
    assert "delete" in delete_cmd
    assert "--min-age" in delete_cmd
    assert "30d" in delete_cmd


async def test_offsite_rclone_retention_zero_skips_prune():
    run = AsyncMock(side_effect=[(0, "", "")])
    with (
        patches_context(
            _patch_settings(
                offsite_backend="rclone",
                offsite_remote="b2:atlas/db",
                offsite_retention_days=0,
            )
        ),
        patch("core.backup_offsite.shutil.which", return_value="/usr/bin/rclone"),
        patch("core.backup_offsite._run", new=run),
    ):
        result = await off.offsite_sync()
    assert result["ok"] is True
    assert result["pruned"] == 0
    # Only one subprocess call (copy); delete was skipped at retention=0.
    assert run.await_count == 1


async def test_offsite_rclone_copy_failure_reports_not_raises():
    run = AsyncMock(return_value=(1, "", "auth failed"))
    with (
        patches_context(
            _patch_settings(
                offsite_backend="rclone", offsite_remote="b2:atlas/db"
            )
        ),
        patch("core.backup_offsite.shutil.which", return_value="/usr/bin/rclone"),
        patch("core.backup_offsite._run", new=run),
    ):
        result = await off.offsite_sync()
    assert result["ok"] is False
    assert "auth failed" in result["reason"]


async def test_offsite_unknown_backend_reports():
    with patches_context(_patch_settings(offsite_backend="aws")):
        result = await off.offsite_sync()
    assert result["ok"] is False
    assert "unknown backend" in result["reason"]


async def test_offsite_sync_job_swallows_exceptions():
    """The scheduler entry point must never raise."""
    with patch(
        "core.backup_offsite.offsite_sync",
        new=AsyncMock(side_effect=RuntimeError("explosion")),
    ):
        await off.offsite_sync_job()  # must not raise


async def test_daily_backup_job_chains_offsite():
    """daily_backup_job calls offsite_sync_job after the local dump."""
    from pathlib import Path

    from core import backup

    fake_path = Path("/tmp/atlas-fake.dump")

    class FakeStat:
        st_size = 1024 * 1024  # 1 MB

    with (
        patch(
            "core.backup.run_backup", new=AsyncMock(return_value=fake_path)
        ),
        patch.object(fake_path.__class__, "stat", return_value=FakeStat()),
        patch("core.backup.prune_old_backups", return_value=[]),
        patch(
            "core.backup_offsite.offsite_sync_job",
            new=AsyncMock(),
        ) as offsite,
    ):
        await backup.daily_backup_job()
    offsite.assert_awaited_once()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from contextlib import contextmanager  # noqa: E402


@contextmanager
def patches_context(patch_objs):
    """Enter a list of patches like a with-statement, exit them on cleanup."""
    started = []
    try:
        for p in patch_objs:
            started.append(p.__enter__())
        yield started
    finally:
        for p in reversed(patch_objs):
            try:
                p.__exit__(None, None, None)
            except Exception:
                pass
