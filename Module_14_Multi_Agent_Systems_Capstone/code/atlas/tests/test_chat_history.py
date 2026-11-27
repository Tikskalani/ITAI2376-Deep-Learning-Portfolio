"""The Chat channel keeps its conversations (core/chat_history.py, migration 036).

No model call: the CEO's stream is faked. Tables are shadowed.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from core import chat_history, db
from core.config import REPO_ROOT
from core.web.api import app
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")
TABLES = ("chat_threads", "chat_messages", "events")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas")


@pytest.fixture
def fake_ceo(monkeypatch):
    """dispatch_stream answers from a script and records what context it was given."""
    seen: list[dict[str, Any]] = []

    async def stream(agent: str, task: str, **kw: Any):
        seen.append({"task": task, **kw})
        yield {"type": "delta", "text": "Here is "}
        yield {"type": "delta", "text": "the plan."}
        yield {"type": "done", "text": "Here is the plan.<tool_call>x</tool_call>", "episode_id": None,
               "turns": 1, "stop_reason": "end_turn"}

    monkeypatch.setattr("core.runtime.dispatcher.dispatch_stream", stream)
    return seen


async def _send(c: httpx.AsyncClient, text: str, **body: Any) -> list[dict[str, Any]]:
    r = await c.post("/api/chat/stream", json={"message": text, **body})
    assert r.status_code == 200
    return [json.loads(line[5:]) for line in r.text.split("\n") if line.startswith("data:")]


async def test_a_conversation_is_kept_and_continues(fake_ceo, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES), _client() as c:
        first = await _send(c, "Plan my day.")
        tid = first[0]["id"]
        assert first[0]["type"] == "thread" and first[-1]["type"] == "done"
        second = await _send(c, "And the second item?")
        assert second[0]["id"] == tid, "a warm thread continues"
        threads = (await c.get("/api/chat/threads")).json()["threads"]
        thread = (await c.get(f"/api/chat/threads/{tid}")).json()
    assert [t["title"] for t in threads] == ["Plan my day."] and threads[0]["messages"] == 4
    assert [(m["role"], m["body"]) for m in thread["messages"]] == [
        ("you", "Plan my day."), ("ceo", "Here is the plan."),
        ("you", "And the second item?"), ("ceo", "Here is the plan.")]
    assert fake_ceo[0]["memory_context"] is None
    assert fake_ceo[1]["memory_context"].startswith("Earlier in this conversation:")
    assert "Trilok: Plan my day." in fake_ceo[1]["memory_context"]
    assert fake_ceo[1]["thread_id"] == tid


async def test_new_chat_and_a_cold_thread_start_fresh(fake_ceo, monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES), _client() as c:
        a = (await _send(c, "One."))[0]["id"]
        b = (await _send(c, "Two.", new_thread=True))[0]["id"]
        await db.execute("update chat_threads set last_at = now() - $1::interval", chat_history.THREAD_IDLE + timedelta(minutes=1))
        cold = (await _send(c, "Three."))[0]["id"]
        named = (await _send(c, "Back to one.", thread_id=a))[0]["id"]
    assert len({a, b, cold}) == 3 and named == a


async def test_unknown_threads(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES), _client() as c:
        assert (await c.get("/api/chat/threads/not-a-thread")).status_code == 404
        assert (await c.get("/api/chat/threads/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_context_keeps_the_latest_turns_within_the_cap(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        tid = await chat_history.thread_for(None, new=True, first="x")
        for i in range(10):
            await chat_history.add(tid, "you" if i % 2 == 0 else "ceo", f"turn {i} " + "w" * 900)
        ctx = await chat_history.context(tid)
    assert ctx is not None and "turn 9" in ctx and "turn 0" not in ctx
    assert len(ctx) <= chat_history.CONTEXT_CHARS + 200


def test_migration_and_probe_agree():
    from core.web import api

    # The probe follows the newest migration; 036 creates the chat tables either way.
    migrations = sorted((REPO_ROOT / "db" / "migrations").glob("*.sql"))
    newest, obj = api.SCHEMA_PROBE
    assert newest == migrations[-1].name and obj in migrations[-1].read_text(encoding="utf-8")
    assert "chat_messages_thread_idx" in (REPO_ROOT / "db" / "migrations" / "036_chat_threads.sql").read_text(encoding="utf-8")
