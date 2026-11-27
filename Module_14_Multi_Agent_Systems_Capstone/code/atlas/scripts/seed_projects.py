"""Seed the projects table with example projects (the real list is not part of this package).

    python -m scripts.seed_projects            # seed and print the block
    python -m scripts.seed_projects --print    # print only
"""

from __future__ import annotations

import argparse
import asyncio

from core.projects import projects_block, upsert_project

# slug, name, kind, priority, owner_agent, objective, repos
SEED: list[tuple[str, str, str, int, str, str, list[str]]] = [
    ("demo-dental", "Demo Dental", "client", 1, "client_delivery",
     "Deliver the practice's inventory system and keep the client informed.", []),
    ("itai-2376", "ITAI 2376 capstone", "college", 1, "academic",
     "Submit the AI Agent Creation capstone by Dec 4.", []),
    ("job-search", "Job search", "personal", 2, "career",
     "Land an applied AI engineering role; track applications and follow-ups.", []),
    ("household", "Household", "own", 3, "personal_ops", "Bills paid on time.", []),
]


async def main(print_only: bool) -> None:
    if not print_only:
        for slug, name, kind, prio, owner, objective, repos in SEED:
            await upsert_project(slug, name, kind=kind, priority=prio, owner_agent=owner,
                                 objective=objective, repos=repos)
    print(await projects_block())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    asyncio.run(main(ap.parse_args().print))
