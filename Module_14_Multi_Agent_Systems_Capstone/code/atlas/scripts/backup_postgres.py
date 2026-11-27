"""Run a Postgres backup on demand.

Same job the scheduler runs daily, exposed as a CLI for ad-hoc backups
or to seed a known good state before risky changes.

Usage:
    python -m scripts.backup_postgres

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from core.backup import BackupError, prune_old_backups, run_backup


async def _main() -> int:
    try:
        path = await run_backup()
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"backup written: {path} ({size_mb:.2f} MB)")

    removed = prune_old_backups()
    if removed:
        print(
            f"pruned {len(removed)} old backup(s): "
            + ", ".join(p.name for p in removed)
        )
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
