"""The knowledge base from Claude (core/kb.py): an export in, what is
durable about him in memory."""

from __future__ import annotations

import io
import json
import zipfile
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, kb
from core.memory import semantic
from core.projects import upsert_project
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"X-Atlas-Client": "1", "Origin": "http://atlas"}


def _conv(uuid: str, name: str, *turns: str, created: str = "2026-09-20T10:00:00Z") -> dict:
    msgs = []
    for n, text in enumerate(turns):
        msgs.append({"uuid": f"{uuid}-{n}", "sender": "human" if n % 2 == 0 else "assistant", "text": text,
                     "content": [{"type": "text", "text": text}], "created_at": created})
    return {"uuid": uuid, "name": name, "created_at": created, "chat_messages": msgs}


LONG = ("I bought a house in Katy last year and the mortgage is $2,480 on the 1st; " * 6).strip()
CONVERSATIONS = [
    _conv("c-1", "Mortgage planning", LONG, "Noted. " * 40, "Also my Tesla Model Y lease ends in March 2027. " * 3),
    _conv("c-2", "hi", "hi", "Hello!"),                                    # too short to hold anything
    _conv("c-3", "Sidework launch", "Sidework is my side project, an AI concierge for small landlords; I registered sidework.ai on Oct 2. " * 4,
          "Great. " * 30),
]
PROJECTS = [{"uuid": "p-1", "name": "Sidework", "description": "AI concierge for small landlords",
             "prompt_template": "Answer as the Sidework product lead.",
             "docs": [{"filename": "decisions.md", "content": "Decided on Postgres over Supabase on Sep 30." * 20}]}]
MEMORIES = [{"uuid": "m-1", "content": "Trilok prefers short replies and plain prose, no bullet points."}]


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("conversations.json", json.dumps(CONVERSATIONS))
        zf.writestr("projects.json", json.dumps(PROJECTS))
        zf.writestr("memories.json", json.dumps(MEMORIES))
        zf.writestr("users.json", json.dumps([{"email": "t@example.com"}]))
    return buf.getvalue()


# ------------------------------------------------------------------ reading the export


def test_the_export_is_read_from_a_zip_or_a_bare_json():
    out = kb.read_export(_zip())
    assert [c["uuid"] for c in out["conversations"]] == ["c-1", "c-2", "c-3"] and len(out["projects"]) == 1 and len(out["memories"]) == 1
    bare = kb.read_export(json.dumps(CONVERSATIONS).encode(), "conversations.json")
    assert len(bare["conversations"]) == 3 and bare["projects"] == []
    assert len(kb.read_export(json.dumps(PROJECTS).encode(), "projects.json")["projects"]) == 1
    with pytest.raises(ValueError):
        kb.read_export(b"not json at all")
    with pytest.raises(ValueError):
        kb.read_export(json.dumps({"other": 1}).encode())
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        kb.read_export(empty.getvalue())


def test_items_are_bounded_titled_and_skip_the_one_liners():
    items = kb.items_of(kb.read_export(_zip()))
    assert [(i["kind"], i["ref"]) for i in items] == [("conversation", "c-1"), ("conversation", "c-3"), ("project", "p-1"), ("memory", "m-1")]
    first = items[0]
    assert first["title"] == "Mortgage planning" and first["text"].startswith("Conversation: Mortgage planning (2026-09-20)")
    assert "Trilok: I bought a house" in first["text"] and "Claude: Noted." in first["text"]
    assert items[2]["text"].startswith("Project: Sidework") and "Document decisions.md" in items[2]["text"]
    assert items[3]["text"].startswith("What Claude remembered")
    big = _conv("c-big", "Long", "x" * 20000, "y" * 20000)
    _, text, chars = kb.conversation_text(big)
    assert chars > 40000 and len(text) < kb.HEAD_CHARS + kb.TAIL_CHARS + 200 and "left out" in text
    est = kb.estimate_usd(items)
    assert Decimal("0.01") <= est <= Decimal("0.10") and kb.estimate_usd([]) == Decimal("0.00")


# ------------------------------------------------------------------ the import


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async def fake_embed(text: str) -> list[float]:
        return [0.01] * 768
    monkeypatch.setattr(semantic, "embed", fake_embed)
    async with livedb.shadow(monkeypatch, "kb_imports", "kb_items", "facts", "profile_notes", "projects", "llm_calls"):
        await upsert_project("sidework", "Sidework", kind="own")
        yield


def _choose(by_ref: dict[str, dict]):
    calls: list[str] = []

    async def choose(**kw):
        user = kw["user"]
        calls.append(user[:40])
        for ref, answer in by_ref.items():
            if ref in user:
                return answer
        return {"skip": True, "facts": []}
    choose.calls = calls  # type: ignore[attr-defined]
    return choose


ANSWERS = {
    "Mortgage planning": {"skip": False, "facts": [
        {"content": "Trilok owns a house in Katy, Texas, bought in 2025, with a $2,480 mortgage payment due on the 1st.", "kind": "fact", "about": "him", "confidence": 0.95},
        {"content": "Trilok's Tesla Model Y lease ends in March 2027.", "kind": "constraint", "about": "him", "confidence": 0.9},
        {"content": "Trilok's password is hunter2", "kind": "fact", "about": "him", "confidence": 0.9},
        {"content": "Maybe he likes trains", "kind": "preference", "about": "him", "confidence": 0.3},
    ]},
    "Sidework launch": {"skip": False, "facts": [
        {"content": "Sidework is Trilok's side project: an AI concierge for small landlords, with sidework.ai registered on October 2, 2026.", "kind": "project", "about": "Sidework", "confidence": 0.9},
    ]},
    "Project: Sidework": {"skip": False, "facts": [
        {"content": "For Sidework, Trilok decided on Postgres over Supabase on September 30, 2026.", "kind": "decision", "about": "Sidework", "confidence": 0.92},
        {"content": "Sidework is Trilok's side project, an AI concierge for small landlords; sidework.ai was registered October 2, 2026.", "kind": "project", "about": "Sidework", "confidence": 0.9},
    ]},
    "What Claude remembered": {"skip": False, "facts": [
        {"content": "Trilok prefers short replies in plain prose, without bullet points.", "kind": "preference", "about": "him", "confidence": 0.95},
    ]},
}


@aio
async def test_an_export_becomes_facts_profile_lines_and_project_links(world):
    queued = await kb.enqueue(_zip(), "data-2026-10-11.zip")
    assert queued["items"] == 4 and queued["status"] == "queued" and queued["already_read"] == 0
    assert queued["metadata"] == {"conversations": 3, "projects": 1, "memories": 1} and Decimal(queued["cap_usd"]) == Decimal("10.00")
    with pytest.raises(ValueError, match="already running"):
        await kb.enqueue(_zip(), "again.zip")

    chooser = _choose(ANSWERS)
    out = await kb.run_batch(limit=3, choose=chooser)
    assert out["read"] == 3 and out["status"] == "running" and out["left"] == 1
    out = await kb.run_batch(choose=chooser)
    assert out["read"] == 1 and out["status"] == "done" and out["left"] == 0
    assert len(chooser.calls) == 4 and all(c.startswith(("Conversation", "Project", "What Claude")) for c in chooser.calls)

    facts = [dict(r) for r in await db.fetch("select content, source, confidence, project_id from facts order by created_at")]
    contents = [f["content"] for f in facts]
    assert any(c.startswith("Trilok owns a house in Katy") for c in contents)
    assert not any("hunter2" in c for c in contents)                               # a secret is dropped
    assert not any("trains" in c for c in contents)                                # low confidence is dropped
    assert sum(1 for c in contents if "AI concierge for small landlords" in c) == 1  # the near-duplicate is skipped
    pick = next(f for f in facts if "AI concierge" in f["content"])
    assert pick["source"] == "claude:c-3" and pick["project_id"] is not None
    assert next(f for f in facts if "Postgres over Supabase" in f["content"])["project_id"] == pick["project_id"]
    notes = [dict(r) for r in await db.fetch("select kind, content, source from profile_notes order by created_at")]
    assert [n["kind"] for n in notes] == ["constraint", "decision", "preference"] and {n["source"] for n in notes} == {"claude-import"}

    imp = await kb.get(queued["id"])
    assert imp["status"] == "done" and imp["done"] == 4 and imp["facts"] == 5 and imp["notes"] == 3 and imp["finished_at"]
    items = {r["ref"]: r["status"] for r in await db.fetch("select ref, status from kb_items")}
    assert items == {"c-1": "done", "c-3": "done", "p-1": "done", "m-1": "done"}

    seen = await kb.status()
    assert seen["facts_total"] == 5 and seen["import"]["id"] == queued["id"] and len(seen["recent"]) == 5

    # The same export dropped again: nothing is read twice.
    again = await kb.enqueue(_zip(), "same.zip")
    assert again["items"] == 0 and again["already_read"] == 4 and again["skipped"] == 4
    assert (await kb.run_batch(choose=chooser))["status"] == "done" and len(chooser.calls) == 4


@aio
async def test_the_cap_pauses_the_import_and_a_raise_resumes_it(world):
    conv = _conv("c-9", "Trip", "We are flying to Lisbon on December 20 for ten days. " * 8, "Nice. " * 40)
    queued = await kb.enqueue(json.dumps([conv]).encode(), "conversations.json", cap_usd=0.05)
    await db.execute("update kb_imports set started_at = now() - interval '1 minute', status = 'running' where id = $1", queued["id"])
    await db.execute("insert into llm_calls (purpose, model, tier, usage, cost_usd, called_at) values ('kb_import', 'claude-haiku-5-5', 'scan', '{}', 0.06, now())")
    out = await kb.run_batch(choose=_choose({}))
    assert out["status"] == "paused" and out["read"] == 0
    imp = await kb.get(queued["id"])
    assert imp["status"] == "paused" and "cap" in imp["error"] and Decimal(imp["cost_usd"]) == Decimal("0.06")
    resumed = await kb.resume(queued["id"], cap_usd=1.0)
    assert resumed["status"] == "queued" and Decimal(resumed["cap_usd"]) == Decimal("1.00") and resumed["error"] is None
    out = await kb.run_batch(choose=_choose({"Trip": {"skip": False, "facts": [
        {"content": "Trilok is flying to Lisbon on December 20, 2026 for ten days.", "kind": "constraint", "about": "him", "confidence": 0.9}]}}))
    assert out["status"] == "done" and out["facts"] == 1
    assert (await kb.pause(queued["id"]))["status"] == "done"                      # nothing to pause once done

    async def broken(**kw):
        raise RuntimeError("api down")
    queued2 = await kb.enqueue(json.dumps([_conv("c-10", "Other", "Something about his HOA dues of $90 a month. " * 8, "Ok. " * 40)]).encode(), "c.json")
    out = await kb.run_batch(choose=broken)
    assert out["read"] == 1 and out["status"] == "done"
    assert (await db.fetchval("select status from kb_items where ref = 'c-10'")) == "failed"
    assert (await kb.get(queued2["id"]))["status"] == "done"


@aio
async def test_routes(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        r = await c.get("/api/connect/claude")
        assert r.status_code == 200 and r.json()["import"] is None
        r = await c.post("/api/connect/claude", files={"file": ("data.zip", _zip(), "application/zip")}, data={"cap_usd": "3"})
        assert r.status_code == 200 and r.json()["items"] == 4 and float(r.json()["cap_usd"]) == 3.0
        iid = r.json()["id"]
        assert (await c.post("/api/connect/claude", files={"file": ("x.zip", _zip(), "application/zip")})).status_code == 422
        assert (await c.post("/api/connect/claude", files={"file": ("e.zip", b"", "application/zip")})).status_code == 422
        assert (await c.post(f"/api/connect/claude/{iid}/pause")).json()["status"] == "paused"
        r = await c.post(f"/api/connect/claude/{iid}/resume", json={"cap_usd": 7})
        assert r.json()["status"] == "queued" and float(r.json()["cap_usd"]) == 7.0
        assert (await c.post(f"/api/connect/claude/{uuid4()}/pause")).status_code == 404
        r = await c.get("/api/connect/claude")
        assert r.json()["import"]["id"] == iid and r.json()["facts_total"] == 0
