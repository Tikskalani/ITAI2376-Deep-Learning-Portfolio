"""Restore-test a pg_dump backup.

Restores a `.dump` file into a freshly-created test database and
compares per-table row counts against the live atlas database. Exits 0
when every table matches, 1 on drift or restore failure.

Until a backup has been restored end to end at least once, the daily
pg_dump job is just theatre. Run this script after any backup-format
change, after restoring on a new machine, and at least once on the
first dump from production.

Usage:
    python -m scripts.restore_postgres path/to/atlas-YYYY-MM-DD.dump
    python -m scripts.restore_postgres path/to/atlas-YYYY-MM-DD.dump --cleanup

--cleanup drops the test database after the comparison. Without it the
test database is left in place for manual inspection.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from core.config import settings
from core.restore import (
    RestoreError,
    compare_counts,
    drop_test_db,
    restore_to_test_db,
    row_counts,
)


async def _main(dump_path: Path, cleanup: bool) -> int:
    print(f"Reading row counts from live database: {settings.pg_db}")
    try:
        source = await row_counts(settings.pg_db)
    except RestoreError as exc:
        print(f"could not read live counts: {exc}", file=sys.stderr)
        return 1
    print(f"  {len(source)} tables in source")

    print(f"Restoring {dump_path.name} into a test database...")
    try:
        test_db = await restore_to_test_db(dump_path)
    except RestoreError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"  restored into: {test_db}")

    print("Reading row counts from restored database...")
    try:
        restored = await row_counts(test_db)
    except RestoreError as exc:
        print(f"could not read restored counts: {exc}", file=sys.stderr)
        if cleanup:
            await drop_test_db(test_db)
        return 1

    report = compare_counts(source, restored)
    drift = [r for r in report if r["status"] != "match"]

    print()
    header = f"{'Table':<30} {'Source':>10} {'Restored':>10} {'Status':<22}"
    print(header)
    print("-" * len(header))
    for r in report:
        s = "-" if r["source"] is None else str(r["source"])
        t = "-" if r["restored"] is None else str(r["restored"])
        print(f"{r['table']:<30} {s:>10} {t:>10} {r['status']:<22}")
    print()

    if not drift:
        print(f"OK: all {len(report)} tables match.")
        rc = 0
    else:
        print(f"DRIFT: {len(drift)} of {len(report)} tables differ.")
        rc = 1

    if cleanup:
        print(f"Dropping test database {test_db}...")
        await drop_test_db(test_db)
    else:
        print(f"Test database {test_db} left in place. Drop with:")
        print(
            f"  docker exec atlas-postgres dropdb -U {settings.pg_user} "
            f"{test_db}"
        )
    return rc


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(
            "usage: python -m scripts.restore_postgres "
            "<dump-file> [--cleanup]",
            file=sys.stderr,
        )
        sys.exit(2)
    dump_path = Path(args[0])
    cleanup = "--cleanup" in args[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sys.exit(asyncio.run(_main(dump_path, cleanup)))


if __name__ == "__main__":
    main()
