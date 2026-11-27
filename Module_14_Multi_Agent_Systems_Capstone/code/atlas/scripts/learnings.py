"""Review and implement learning candidates (Gate 2).

  python -m scripts.learnings list [status]      # default: proposed
  python -m scripts.learnings draft <id>         # CTO drafts the playbook (Sonnet)
  python -m scripts.learnings adopt <id>         # skill into trial (+ procedure if drafted)
  python -m scripts.learnings reject <id>
  python -m scripts.learnings skills [status]    # the playbook in force, with its numbers
  python -m scripts.learnings retire <skill_id> [reason]
  python -m scripts.learnings keep <skill_id>    # trial -> active

Typical flow: tap Pursue in the PWA (the CTO drafts in the background), read
the draft, Adopt. Adoption creates a skill in trial that is injected into
every run of the agents it names, writes a procedure when the draft has
one, and records a lesson. Nothing touches code; agents prepare, you
approve. The Sunday review reports each skill's uses and ratings.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from core import db, discovery
from core import learning_implement as li


def _short(u) -> str:
    return str(u)[:8]


async def _list(status: str) -> None:
    rows = await discovery.list_candidates(status=status, limit=50)
    if not rows:
        print(f"no candidates with status {status!r}")
        return
    for r in rows:
        rel = r["relevance"]
        rel_s = f"{round(float(rel) * 100)}%" if rel is not None else "  -"
        print(f"[{_short(r['id'])}] {status:9} rel {rel_s:>4}  "
              f"{(r['creator'] or 'unknown')[:16]:16}  {(r['learning'] or '')[:70]}")


async def _draft(raw_id: str) -> None:
    print("drafting via CTO (Sonnet, a few cents)...")
    draft = await li.draft_candidate(UUID(raw_id))
    print(f"\nkind:       {draft.get('kind')}")
    print(f"applies to: {', '.join(draft.get('applies_to') or [])}")
    print(f"domain:     {draft.get('domain')}")
    print(f"title:      {draft.get('title')}")
    print("\n--- playbook ---\n")
    print(draft.get("playbook") or "")
    if draft.get("body"):
        print("\n--- body ---\n")
        print(draft.get("body") or "")
    print("\n--- end ---")
    if draft.get("kind") != "code_note":
        print(f"\nIf this looks right: python -m scripts.learnings adopt {raw_id}")
    else:
        print("\nThis is a code_note. Implement it by hand; `adopt` will refuse it on purpose.")


async def _adopt(raw_id: str) -> None:
    out = await li.adopt_candidate(UUID(raw_id))
    print(f"adopted. skill {out['skill_id']} in trial for {', '.join(out['applies_to'])}")
    if out.get("path"):
        print(f"wrote and indexed procedure: {out['path']}")


async def _skills(status: str | None) -> None:
    from core import skills
    rows = await skills.list_skills(status=status)
    if not rows:
        print("no skills" + (f" with status {status!r}" if status else ""))
        return
    for r in rows:
        print(f"[{_short(r['id'])}] {r['status']:7} {', '.join(r['applies_to']):24} "
              f"used {r['uses']:3}  good {r['good']:2}  bad {r['bad']:2}  {r['title'][:60]}")


async def _retire(raw_id: str, reason: str | None) -> None:
    from core import skills
    ok = await skills.retire_skill(UUID(raw_id), reason)
    print("retired" if ok else "no such skill")


async def _keep(raw_id: str) -> None:
    from core import skills
    ok = await skills.activate_skill(UUID(raw_id))
    print("kept (active)" if ok else "no such skill")


async def _reject(raw_id: str) -> None:
    await li.reject_candidate(UUID(raw_id))
    print(f"rejected {raw_id}")


async def _run(coro) -> None:
    try:
        await coro
    finally:
        await db.close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(description="Review and implement learning candidates.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lst = sub.add_parser("list", help="list candidates by status (default proposed)")
    lst.add_argument("status", nargs="?", default="proposed")
    for name in ("draft", "adopt", "reject", "keep"):
        sp = sub.add_parser(name)
        sp.add_argument("id")
    sk = sub.add_parser("skills", help="list skills (optionally by status)")
    sk.add_argument("status", nargs="?", default=None)
    rt = sub.add_parser("retire")
    rt.add_argument("id")
    rt.add_argument("reason", nargs="?", default=None)
    args = ap.parse_args()

    if args.cmd == "draft":
        # Dispatching the CTO needs the agents registered in this process.
        from core.runtime.register_all import register_all
        register_all()
        asyncio.run(_run(_draft(args.id)))
    elif args.cmd == "adopt":
        asyncio.run(_run(_adopt(args.id)))
    elif args.cmd == "reject":
        asyncio.run(_run(_reject(args.id)))
    elif args.cmd == "skills":
        asyncio.run(_run(_skills(args.status)))
    elif args.cmd == "retire":
        asyncio.run(_run(_retire(args.id, args.reason)))
    elif args.cmd == "keep":
        asyncio.run(_run(_keep(args.id)))
    else:
        asyncio.run(_run(_list(args.status)))


if __name__ == "__main__":
    main()
