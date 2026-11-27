"""Manage the trusted sources the nightly discovery scan sweeps.

  python -m scripts.discovery_sources list
  python -m scripts.discovery_sources add "@TwoMinutePapers" --label "Two Minute Papers"
  python -m scripts.discovery_sources add "https://www.youtube.com/@karpathy"
  python -m scripts.discovery_sources off <source-id>

YouTube only for now. 'add' resolves the channel to its RSS channel_id
right away so the first scan does not have to.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from core import db, discovery
from core.youtube import resolve_channel_id


async def _add(handle: str, label: str | None) -> None:
    cid = await resolve_channel_id(handle)
    if not cid:
        print(f"could not resolve {handle!r} to a YouTube channel.")
        print("check the handle, or paste the channel's full URL from your browser, e.g.")
        print('  python -m scripts.discovery_sources add "https://www.youtube.com/@SomeChannel"')
        return
    sid = await discovery.add_source(handle=handle, channel_id=cid, label=label)
    print(f"added {handle}  id={sid}  channel_id={cid}")


async def _list() -> None:
    rows = await discovery.list_sources(active_only=False)
    if not rows:
        print("no sources yet -- add one with: python -m scripts.discovery_sources add \"@handle\"")
        return
    for r in rows:
        flag = "on " if r["active"] else "off"
        print(f"[{flag}] {r['label'] or r['handle']}  ({r['handle']})  "
              f"channel_id={r['channel_id'] or '-'}  "
              f"last_scanned={r['last_scanned_at'] or 'never'}  id={r['id']}")


async def _off(source_id: str) -> None:
    await discovery.deactivate_source(UUID(source_id))
    print(f"deactivated {source_id}")


async def _run(coro) -> None:
    try:
        await coro
    finally:
        await db.close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage discovery sources for the nightly scan.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a YouTube channel (@handle, URL, or UC... id)")
    a.add_argument("handle")
    a.add_argument("--label", default=None)
    sub.add_parser("list", help="list all sources")
    o = sub.add_parser("off", help="deactivate a source by id")
    o.add_argument("source_id")
    args = ap.parse_args()
    if args.cmd == "add":
        asyncio.run(_run(_add(args.handle, args.label)))
    elif args.cmd == "list":
        asyncio.run(_run(_list()))
    elif args.cmd == "off":
        asyncio.run(_run(_off(args.source_id)))


if __name__ == "__main__":
    main()
