"""The Chat channel's memory: threads of what Trilok typed and what the CEO said.

Before this, the channel lived in the page: a reload lost every answer
(audit finding 07). Now each message and reply is stored (migration 036),
the channel opens on the latest thread, and a history list reopens older
ones. A thread continues until THREAD_IDLE passes with no message, or he
starts a new one.

The CEO sees the last few turns of the thread (context()) so a follow-up
("and the second one?") has something to refer to. That text is his own
typing and the CEO's own replies; the CEO only ever saw privacy-gated
content (core/privacy.py), so nothing new reaches the model.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from core import db

THREAD_IDLE = timedelta(hours=6)
CONTEXT_TURNS = 6
CONTEXT_CHARS = 4000
TITLE_CHARS = 80


def _title(text: str) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


async def thread_for(thread_id: str | None, *, new: bool = False, first: str = "") -> UUID:
    """The thread a message belongs to: the one named, else the latest if it
    is still warm, else a new one titled from the message."""
    if thread_id and not new:
        try:
            tid = UUID(str(thread_id))
        except ValueError:
            tid = None
        if tid is not None and await db.fetchval("select 1 from chat_threads where id = $1", tid):
            return tid
    if not new:
        warm = await db.fetchval(
            "select id from chat_threads where last_at > now() - $1::interval order by last_at desc limit 1",
            THREAD_IDLE,
        )
        if warm is not None:
            return UUID(str(warm))
    return UUID(str(await db.fetchval(
        "insert into chat_threads (title) values ($1) returning id", _title(first))))


async def add(thread_id: UUID, role: str, body: str, episode_id: Any = None) -> None:
    await db.execute(
        "insert into chat_messages (thread_id, role, body, episode_id) values ($1, $2, $3, $4)",
        thread_id, role, body, UUID(str(episode_id)) if episode_id else None,
    )
    await db.execute(
        "update chat_threads set last_at = now(), title = case when title = '' then $2 else title end where id = $1",
        thread_id, _title(body) if role == "you" else "",
    )


async def context(thread_id: UUID) -> str | None:
    """The last turns of the thread, for the CEO; None for a fresh thread."""
    rows = await db.fetch(
        "select role, body from chat_messages where thread_id = $1 order by id desc limit $2",
        thread_id, CONTEXT_TURNS,
    )
    if not rows:
        return None
    lines: list[str] = []
    used = 0
    for r in rows:  # newest first, so the most recent turns survive the cut
        line = f"{'Trilok' if r['role'] == 'you' else 'You (the CEO)'}: {' '.join(r['body'].split())}"
        if used + len(line) > CONTEXT_CHARS:
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    return "Earlier in this conversation:\n" + "\n".join(lines)


async def threads(limit: int = 30) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select t.id, t.title, t.started_at, t.last_at, count(m.id) as messages "
        "from chat_threads t left join chat_messages m on m.thread_id = t.id "
        "group by t.id order by t.last_at desc limit $1",
        max(1, min(int(limit), 200)),
    )
    return [{"id": str(r["id"]), "title": r["title"] or "Conversation", "started_at": r["started_at"],
             "last_at": r["last_at"], "messages": int(r["messages"])} for r in rows]


async def messages(thread_id: str) -> dict[str, Any] | None:
    try:
        tid = UUID(str(thread_id))
    except ValueError:
        return None
    head = await db.fetchrow("select id, title, started_at, last_at from chat_threads where id = $1", tid)
    if head is None:
        return None
    rows = await db.fetch(
        "select id, role, body, episode_id, created_at from chat_messages where thread_id = $1 order by id", tid)
    return {"id": str(head["id"]), "title": head["title"] or "Conversation", "started_at": head["started_at"],
            "last_at": head["last_at"],
            "messages": [{"id": r["id"], "role": r["role"], "body": r["body"],
                          "episode_id": str(r["episode_id"]) if r["episode_id"] else None,
                          "created_at": r["created_at"]} for r in rows]}


__all__ = ["THREAD_IDLE", "add", "context", "messages", "thread_for", "threads"]
