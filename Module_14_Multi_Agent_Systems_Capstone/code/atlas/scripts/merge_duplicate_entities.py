"""Atlas -- merge duplicate entities by canonical_name.

The original upsert_entity matched on (type, canonical_name), so the
same name extracted as different types created multiple rows. The fix
in core/knowledge_graph.py prevents new duplicates; this script cleans
up the ones already in the table.

For each canonical_name with more than one row:
  * keep the oldest row (lowest created_at) as the survivor
  * merge each duplicate's metadata.types into the survivor
  * repoint relationships and entity_attributes to the survivor
  * delete the duplicate

Run once, then never again. Idempotent: rerunning is a no-op.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict

from core import db


async def main() -> None:
    rows = await db.fetch(
        """
        select id, type, name, canonical_name, metadata, created_at
        from entities
        order by lower(canonical_name), created_at asc
        """
    )

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["canonical_name"] or r["name"]).strip().lower()
        groups[key].append(dict(r))

    merged = 0
    for canonical, members in groups.items():
        if len(members) < 2:
            continue
        survivor = members[0]
        duplicates = members[1:]
        print(
            f"\ncanonical={canonical!r}: keeping {survivor['type']}/{survivor['name']} "
            f"({str(survivor['id'])[:8]}), merging {len(duplicates)} duplicate(s)"
        )

        seen_types = set(
            (survivor["metadata"] or {}).get("types") or []
        )
        seen_types.add(survivor["type"])

        for dup in duplicates:
            seen_types.add(dup["type"])
            seen_types.update((dup["metadata"] or {}).get("types") or [])
            print(
                f"  - merging dup {dup['type']}/{dup['name']} "
                f"({str(dup['id'])[:8]})"
            )
            await db.execute(
                "update relationships set from_entity = $2 where from_entity = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "update relationships set to_entity = $2 where to_entity = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "update entity_attributes set entity_id = $2 where entity_id = $1",
                dup["id"],
                survivor["id"],
            )
            await db.execute(
                "delete from entities where id = $1", dup["id"]
            )
            merged += 1

        new_meta = dict(survivor["metadata"] or {})
        new_meta["types"] = sorted(seen_types)
        await db.execute(
            "update entities set metadata = $2 where id = $1",
            survivor["id"],
            new_meta,
        )

    print(f"\nDone. {merged} duplicate row(s) merged.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
