"""The one-page master: propose cuts, approve them, check them.

  python -m scripts.resume_one_page propose            run the engine's cut stage once (a few cents)
                                                       and list every bullet, the proposed cuts marked
  python -m scripts.resume_one_page apply B3 B7 B12    approve these cuts (your choice, not the proposal's)
  python -m scripts.resume_one_page show               the approved one-page master and what it cut

The full master in data/resume/ stays the source of truth. Approving writes only
the list of cuts (data/resume/one_page.json) with the master's fingerprint; the
one-page text is rebuilt from the full master every time, and refused if the
master changed since. --dir reads the master from another folder (a worktree).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from core import db
from core.jobs import config as jobs_config
from core.resume_engine import one_page
from core.resume_engine import resume_format as RF


def _print_listing(prop: dict) -> None:
    print(f"Master: {prop['words']} words. One page holds about {prop['page_words']}.")
    role = None
    for b in prop["bullets"]:
        if b["role"] != role:
            role = b["role"]
            print(f"\n{role}")
        mark = "CUT?" if b["proposed_cut"] else "    "
        print(f"  {mark} {b['id']:>4} ({b['words']:>3} words) {b['text'][:110]}")
        if b["why"]:
            print(f"              why: {b['why']}")
    print(f"\nProposed: {' '.join(prop['proposed']) or 'nothing'}"
          f" -> {prop['words_after']} words, {prop['pages_after']} page(s).")
    print(f"Spent ${prop['spent_usd']}." + (f" Stopped: {prop['stopped']}" if prop["stopped"] else ""))
    print("Approve with: python -m scripts.resume_one_page apply <ids you choose>")


async def main(args: argparse.Namespace) -> int:
    directory = Path(args.dir) if args.dir else jobs_config.RESUME_DIR
    out = Path(args.out) if args.out else directory
    try:
        full = RF.master_format(directory)
        if args.cmd == "propose":
            prop = await one_page.propose(full, title=args.title, cap_usd=Decimal(args.cap))
            (out / one_page.PROPOSAL_NAME).write_text(json.dumps(prop, indent=2), encoding="utf-8")
            _print_listing(prop)
        elif args.cmd == "apply":
            text = one_page.apply(full, args.ids, directory)
            print(f"Approved: {len(args.ids)} cut(s), {len(text.split())} words, one page.")
        else:
            shown = one_page.load(full, directory)
            if shown is None:
                print("No one-page master approved yet. Run: python -m scripts.resume_one_page propose")
            else:
                print(shown)
    except (RF.FormatMismatch, one_page.OnePageError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("propose", "apply", "show"))
    p.add_argument("ids", nargs="*", help="bullet ids to cut (apply)")
    p.add_argument("--title", default="AI Engineer", help="the role the cuts should favour")
    p.add_argument("--cap", default="0.05", help="USD cap for the proposal (propose)")
    p.add_argument("--dir", help="folder holding the master resume (default data/resume)")
    p.add_argument("--out", help="folder for the proposal file (default: --dir)")
    sys.exit(asyncio.run(main(p.parse_args())))
