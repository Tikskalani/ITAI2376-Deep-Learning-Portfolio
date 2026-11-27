"""The misses waiting to become eval cases (core/outcomes.py).

    python -m scripts.eval_candidates               list the proposed candidates
    python -m scripts.eval_candidates --export      append them to evals/data/outcomes.jsonl and mark them written
    python -m scripts.eval_candidates --dismiss ID  drop one

An exported line carries the task, the bad result, what Trilok said, and
the episode, which is what a real check needs: turn each into an EvalCase
(core/runtime/evals.py) or an offline case, and the Atlas Standard's
learning row counts it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

from core import db, outcomes
from core.config import REPO_ROOT

OUT = REPO_ROOT / "evals" / "data" / "outcomes.jsonl"


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", action="store_true", help=f"append proposed candidates to {OUT.relative_to(REPO_ROOT)}")
    ap.add_argument("--dismiss", metavar="ID", help="dismiss one candidate by id")
    args = ap.parse_args(argv)
    try:
        if args.dismiss:
            row = await outcomes.set_candidate(UUID(args.dismiss), "dismissed")
            print("dismissed" if row else "no such candidate")
            return 0 if row else 1
        rows = await outcomes.candidates("proposed", limit=200)
        if not rows:
            print("No misses waiting.")
            return 0
        if args.export:
            Path(OUT).parent.mkdir(parents=True, exist_ok=True)
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(outcomes.to_jsonl(rows))
            for c in rows:
                await outcomes.set_candidate(c["id"], "written", suite="outcomes")
            print(f"wrote {len(rows)} candidate(s) to {OUT.relative_to(REPO_ROOT)}")
            return 0
        for c in rows:
            print(f"{c['id']}  {c['created_at']:%Y-%m-%d}  {c['agent'] or '?':<16} {c['kind']:<13} {(c['task'] or '')[:70]}")
            if c["note"]:
                print(f"    he said: {c['note'][:160]}")
        return 0
    finally:
        await db.close_pool()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
