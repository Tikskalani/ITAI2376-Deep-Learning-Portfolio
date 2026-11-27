"""The job search from the command line, on the laptop.

  python -m scripts.jobs                 the funnel and the best found roles
  python -m scripts.jobs --check-boards  which watched boards answer (no writes)
  python -m scripts.jobs --discover      run board discovery and scoring now
  python -m scripts.jobs --score         score found roles still waiting for a score
  python -m scripts.jobs --rescore       score every open role again (after editing the profile)
  python -m scripts.jobs --scan-mail     file job mail from Gmail now
  python -m scripts.jobs --queue         fill and draft today's apply queue now
  python -m scripts.jobs --seed          load config/jobs.yaml applications

The worker runs discovery at 05:15, the apply queue at 07:00, mail every
three hours and the follow-ups at 07:20 on its own; these are for the first run and for
checking. Board names live in config/jobs.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core import db
from core.jobs import config as jobs_config
from core.jobs import store


async def check_boards() -> int:
    import httpx

    from core.jobs.discovery import _HEADERS, _TIMEOUT, fetch_board, keep

    prof = jobs_config.profile()
    bad = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        for c in jobs_config.companies():
            posts, report = await fetch_board(client, c)
            fits = sum(1 for p in posts if keep(p, prof))
            ok = report.startswith("ok")
            bad += int(not ok)
            extra = f", {fits} fit the profile" if ok else ""
            print(f"  {'ok ' if ok else '!! '} {c['name']:<24} {c['source']}/{c['board']}: {report}{extra}")
    if bad:
        print(f"\n{bad} board(s) did not answer. Fix or remove them in config/jobs.yaml "
              "(the slug is the last part of the company's careers URL).")
    return 1 if bad else 0


async def show() -> None:
    counts = await store.funnel()
    print("Funnel: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v) if any(counts.values()) else "No applications tracked yet.")
    for a in await store.list_applications("open", limit=20):
        nxt = f"  next: {a['next_action']}" if a.get("next_action") else ""
        print(f"  {a['status']:<10} {a['company']}, {a['role']}{nxt}")
    spread = await db.fetch(
        "select (score / 10) * 10 as band, count(*) as n from job_postings "
        "where status in ('new', 'shown') and score is not null group by 1 order by 1 desc"
    )
    if spread:
        print("\nScores of open found roles: " + ", ".join(f"{r['band']}s: {r['n']}" for r in spread))
    roles = await store.top_postings(50, 12)
    if roles:
        print("\nBest found roles:")
        for p in roles:
            print(f"  {p['score']:>3}  {p['title']} at {p['company']} ({p.get('location') or '?'})  {p.get('url') or ''}")


async def main(args: argparse.Namespace) -> int:
    if args.check_boards:
        return await check_boards()
    try:
        if args.seed:
            print(f"loaded {await store.seed_from_config(jobs_config.load())} application(s) from config/jobs.yaml")
        if args.discover:
            from core.jobs.discovery import discover

            res = await discover()
            for name, report in sorted(res["boards"].items()):
                print(f"  {'ok ' if report.startswith('ok') else '!! '} {name}: {report}")
            print(f"kept {res['kept']} fitting role(s), {res['new']} new, {res['scored']} scored")
        if args.rescore:
            print(f"cleared {await store.clear_scores()} score(s); scoring again")
        if (args.score or args.rescore) and not args.discover:
            from core.jobs.discovery import score_new

            print(f"scored {await score_new(jobs_config.profile())} role(s)")
        if args.queue:
            from core.jobs.queue import build

            print(f"queue: {await build()}")
        if args.scan_mail:
            from core.jobs.mail import scan

            print(f"mail: {await scan(days=args.days)}")
        await show()
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check-boards", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--score", action="store_true")
    p.add_argument("--rescore", action="store_true")
    p.add_argument("--scan-mail", action="store_true")
    p.add_argument("--queue", action="store_true",
                   help="fill and draft today's apply queue now (uses the career agent)")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--days", type=int, default=60, help="how far back --scan-mail looks (default 60)")
    sys.exit(asyncio.run(main(p.parse_args())))
