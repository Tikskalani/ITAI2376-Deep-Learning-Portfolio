"""Seed the capstone demo database (POSTGRES_DB=atlas_capstone) with made-up projects.

Nothing here is real data. Run from the worktree root:
    ..\\..\\..\\.venv\\Scripts\\python.exe -m capstone.src.seed_demo
"""

from __future__ import annotations

import asyncio
from datetime import date

from core import db
from core.config import settings
from core.projects import upsert_project

PROJECTS = [
    ("itai-2376", "ITAI 2376 capstone (AI agents)", "college", 1, "academic",
     "Finish and submit the AI Agent Creation capstone: report, code and demo video by Dec 4, 11:59pm.",
     "Record the demo video", date(2026, 12, 4)),
    ("job-search", "Job search", "personal", 2, "career",
     "Land an applied AI engineering role starting in January; track applications and follow-ups.",
     "Follow up on two applications", None),
    ("demo-client-site", "Demo client: bakery website", "client", 2, "client_delivery",
     "Ship a small bakery's website with an online order form; weekly status update to the owner.",
     "Send the weekly status update", None),
    ("household", "Household", "own", 3, "personal_ops",
     "Bills paid on time, renewals tracked.", None, None),
]


async def main() -> None:
    assert settings.pg_db == "atlas_capstone", f"refusing to seed {settings.pg_db}"
    for slug, name, kind, prio, owner, objective, nxt, due in PROJECTS:
        await upsert_project(slug, name, kind=kind, priority=prio, owner_agent=owner,
                             objective=objective, next_action=nxt, next_action_due=due)
    print("seeded", len(PROJECTS), "projects into", settings.pg_db)
    await db.close() if hasattr(db, "close") else None


if __name__ == "__main__":
    asyncio.run(main())
