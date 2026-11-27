"""After a week of quiet briefs: did every one say what it did not check, and list the promises?

    python -m scripts.quiet_brief_audit --since 2026-10-22

Read-only, no model call. A quiet brief is a stored morning or evening brief
that says it was written without the model. For each it reports whether the
"Not checked" line and the "Promises you owe" block were there. Both are
expected while any source is disconnected and any promise is owed, which is
the case until Gmail, Calendar, Canvas and GitHub are connected and the
promises are marked Done.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from typing import Any

from core import db

QUIET_MARK = "Written without the model"


async def audit(since: date) -> dict[str, Any]:
    rows = await db.fetch(
        "select kind, created_at, body from briefs where kind in ('morning', 'evening') "
        "and created_at >= $1 order by created_at",
        since,
    )
    quiet = [r for r in rows if QUIET_MARK in (r["body"] or "")]
    items = [{"kind": r["kind"], "at": r["created_at"].isoformat(),
              "not_checked": "Not checked:" in r["body"], "owes": "Promises you owe" in r["body"]}
             for r in quiet]
    return {"briefs": len(rows), "quiet": len(quiet),
            "all_not_checked": all(i["not_checked"] for i in items),
            "all_owes": all(i["owes"] for i in items), "items": items}


async def main(since: date) -> None:
    out = await audit(since)
    print(f"{out['briefs']} briefs since {since}, {out['quiet']} quiet")
    for i in out["items"]:
        print(f"  {i['at'][:16]} {i['kind']}: not-checked line {'yes' if i['not_checked'] else 'NO'}, "
              f"promises {'yes' if i['owes'] else 'NO'}")
    print(f"every quiet brief had the not-checked line: {out['all_not_checked']}; the promises: {out['all_owes']}")
    await db.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    asyncio.run(main(parser.parse_args().since))
