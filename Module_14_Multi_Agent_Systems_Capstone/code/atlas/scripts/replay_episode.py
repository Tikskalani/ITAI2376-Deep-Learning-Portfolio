"""Replay an Atlas episode from the database.

Read-only. Fetches the episode row and every action recorded under it,
prints a chronological transcript-style view to stdout. Pass --json for
the raw structured payload.

Usage:
    python -m scripts.replay_episode <episode_id_or_prefix>
    python -m scripts.replay_episode <episode_id_or_prefix> --json
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from core.replay import format_replay, replay_record


def _json_default(o):
    """Best-effort JSON renderer for the types replay returns (UUID,
    datetime, Decimal, etc.)."""
    return str(o)


async def _main(ref: str, as_json: bool) -> int:
    record = await replay_record(ref)
    if record is None:
        print(f"no episode found matching {ref!r}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(record, default=_json_default, indent=2))
    else:
        print(format_replay(record))
    return 0


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: python -m scripts.replay_episode "
            "<episode_id_or_prefix> [--json]",
            file=sys.stderr,
        )
        sys.exit(2)
    ref = args[0]
    as_json = "--json" in args[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sys.exit(asyncio.run(_main(ref, as_json)))


if __name__ == "__main__":
    main()
