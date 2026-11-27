"""Unit tests for the restore layer.

asyncio.create_subprocess_exec is mocked so no docker, no pg_restore, no
real Postgres runs. The pure compare_counts and test_db_name helpers
are tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.restore import (
    RestoreError,
    compare_counts,
    restore_to_test_db,
    row_counts,
    test_db_name,
)


def _proc(rc: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    p = MagicMock()
    p.returncode = rc
    p.communicate = AsyncMock(return_value=(stdout, stderr))
    return p


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_test_db_name_is_timestamped():
    name = test_db_name(datetime(2026, 5, 27, 4, 19, 5, tzinfo=UTC))
    assert name == "atlas_restore_test_20260527_041905"
    # Names are stable enough not to collide on adjacent seconds.
    earlier = test_db_name(
        datetime(2026, 5, 27, 4, 19, 4, tzinfo=UTC)
    )
    assert earlier != name


def test_compare_counts_matches_when_equal():
    source = {"facts": 12, "deadlines": 3}
    restored = {"facts": 12, "deadlines": 3}
    report = compare_counts(source, restored)
    assert all(r["status"] == "match" for r in report)


def test_compare_counts_flags_drift_and_missing():
    source = {"facts": 12, "deadlines": 3, "secret_table": 7}
    restored = {"facts": 12, "deadlines": 2, "extra_table": 1}
    report = {r["table"]: r for r in compare_counts(source, restored)}

    assert report["facts"]["status"] == "match"
    assert report["deadlines"]["status"] == "drift"
    assert report["secret_table"]["status"] == "missing_after_restore"
    assert report["extra_table"]["status"] == "only_in_restore"


# ---------------------------------------------------------------------------
# row_counts
# ---------------------------------------------------------------------------


async def test_row_counts_returns_per_table():
    # First call lists tables, then one call per table for counts.
    tables_proc = _proc(0, stdout=b"facts\ndeadlines\n")
    facts_proc = _proc(0, stdout=b"12\n")
    deadlines_proc = _proc(0, stdout=b"3\n")
    procs = [tables_proc, facts_proc, deadlines_proc]
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        counts = await row_counts("atlas")
    assert counts == {"facts": 12, "deadlines": 3}


async def test_row_counts_raises_when_listing_fails():
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_proc(2, stderr=b"connection refused")),
    ):
        with pytest.raises(RestoreError) as exc:
            await row_counts("atlas")
    assert "connection refused" in str(exc.value)


# ---------------------------------------------------------------------------
# restore_to_test_db
# ---------------------------------------------------------------------------


async def test_restore_missing_dump_raises(tmp_path: Path):
    with pytest.raises(RestoreError) as exc:
        await restore_to_test_db(tmp_path / "nope.dump")
    assert "not found" in str(exc.value)


async def test_restore_happy_path(tmp_path: Path):
    dump = tmp_path / "atlas-test.dump"
    dump.write_bytes(b"PGDMP fake binary content")

    # Sequence: dropdb (drop existing), createdb, pg_restore.
    procs = [_proc(0), _proc(0), _proc(0)]
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        name = await restore_to_test_db(dump, test_db="atlas_restore_test_abc")
    assert name == "atlas_restore_test_abc"


async def test_restore_createdb_failure_raises(tmp_path: Path):
    dump = tmp_path / "atlas-test.dump"
    dump.write_bytes(b"fake dump")
    procs = [_proc(0), _proc(1, stderr=b"permission denied")]
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        with pytest.raises(RestoreError) as exc:
            await restore_to_test_db(dump)
    assert "permission denied" in str(exc.value)


async def test_restore_tolerates_pg_restore_warnings(tmp_path: Path):
    """pg_restore exits non-zero for harmless warnings (extension already
    installed). We only fail on FATAL or 'could not'."""
    dump = tmp_path / "atlas-test.dump"
    dump.write_bytes(b"fake dump")
    # dropdb, createdb, pg_restore (warning).
    procs = [
        _proc(0),
        _proc(0),
        _proc(1, stderr=b"warning: extension already exists, skipping"),
    ]
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        # No raise: warning-only is acceptable.
        name = await restore_to_test_db(dump, test_db="atlas_restore_test_x")
    assert name == "atlas_restore_test_x"


async def test_restore_raises_on_fatal_pg_restore(tmp_path: Path):
    dump = tmp_path / "atlas-test.dump"
    dump.write_bytes(b"fake dump")
    procs = [
        _proc(0),
        _proc(0),
        _proc(1, stderr=b"pg_restore: error: could not read from input file"),
    ]
    with patch(
        "core.restore.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        with pytest.raises(RestoreError) as exc:
            await restore_to_test_db(dump)
    assert "could not read" in str(exc.value)
