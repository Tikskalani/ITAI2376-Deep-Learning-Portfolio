"""Atlas -- the always-loaded profile.

A few lines every agent run sees, after the projects block: standing
facts about Trilok that never change, decisions in force, preferences,
and constraints with an expiry ("no new client work until Nov 15"). This
is the tiered-memory pattern every long-lived assistant converges on
(OpenClaw's USER.md and MEMORY.md, Letta's core blocks, Anthropic's
memory tool): keep the always-on part a few hundred tokens, retrieve
everything else.

The CEO maintains it with set_profile_note; the consolidation job drops
expired constraints. The block is cached by the model only as part of
the user turn, so editing a note changes the next run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from core import db

log = logging.getLogger(__name__)

KINDS = ("fact", "decision", "constraint", "preference")
_MAX_BLOCK_CHARS = 1800
_LABEL = {"fact": "Fact", "decision": "Decision", "constraint": "Constraint", "preference": "Preference"}


async def set_note(
    content: str,
    *,
    kind: str = "fact",
    expires_in_days: int | None = None,
    source: str = "owner",
) -> UUID:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    content = (content or "").strip()
    if not content:
        raise ValueError("content must not be empty")
    if len(content) > 300:
        raise ValueError("a profile note is one line; keep it under 300 characters")
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
    if kind == "constraint" and expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=30)  # constraints always expire
    return await db.fetchval(
        """
        insert into profile_notes (kind, content, source, expires_at)
        values ($1, $2, $3, $4) returning id
        """,
        kind,
        content,
        source,
        expires_at,
    )


async def remove_note(note_id: UUID) -> bool:
    res = await db.execute("delete from profile_notes where id = $1", note_id)
    return res.endswith("1")


async def list_notes(*, include_expired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_expired else "where expires_at is null or expires_at > now()"
    rows = await db.fetch(
        f"select * from profile_notes {where} order by kind, created_at"
    )
    return [dict(r) for r in rows]


async def expire_notes() -> int:
    """Consolidation: delete constraints past their date. Returns the count."""
    rows = await db.fetch(
        "delete from profile_notes where expires_at is not null and expires_at <= now() returning id"
    )
    return len(rows)


def format_block(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return ""
    lines = []
    for n in notes:
        line = f"- {_LABEL.get(n['kind'], n['kind'])}: {n['content']}"
        if n.get("expires_at"):
            line += f" (until {n['expires_at'].astimezone(UTC).date().isoformat()})"
        lines.append(line)
    block = "\n".join(lines)
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[:_MAX_BLOCK_CHARS].rsplit("\n", 1)[0] + "\n- (more notes trimmed; recall_facts has the rest)"
    return block


async def profile_block() -> str:
    try:
        return format_block(await list_notes())
    except Exception:
        log.exception("could not load the profile block")
        return ""


__all__ = ["KINDS", "set_note", "remove_note", "list_notes", "expire_notes", "format_block", "profile_block"]
