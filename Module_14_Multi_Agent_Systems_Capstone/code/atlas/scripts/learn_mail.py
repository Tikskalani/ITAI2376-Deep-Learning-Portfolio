"""Learn who matters, what is noise and what is open from Gmail, now.

    python -m scripts.learn_mail                  full pass (180 days)
    python -m scripts.learn_mail --loops          open loops only (21 days)
    python -m scripts.learn_mail --if-older-than 7  full pass only when stale

The worker does this on its own (daily 06:40, weekly Sunday 05:00); this is
for the first run after connecting Gmail, or to see the result right away.
Writes data/context.learned.yaml; config/context.yaml still wins.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core import db
from core.mail_learning import gmail_configured, learn, learned_age_days, refresh_open_loops


async def _main(args: argparse.Namespace) -> int:
    if not gmail_configured():
        print("Gmail is not connected yet (ATLAS_GOOGLE_* in .env). Nothing to learn.")
        return 0
    if args.if_older_than is not None:
        age = learned_age_days()
        if age is not None and age < args.if_older_than:
            print(f"Learned {age:.1f} days ago; nothing to do.")
            return 0
    try:
        summary = await (refresh_open_loops() if args.loops else learn())
    finally:
        await db.close_pool()
    print("Learned from Gmail:", ", ".join(f"{k} {v}" for k, v in summary.items()))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", action="store_true", help="open loops only")
    parser.add_argument("--if-older-than", type=float, default=None, metavar="DAYS")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
