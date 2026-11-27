"""What Atlas believes about him (GET /api/memory and the edits), with the
embedding model replaced and private copies of facts and profile notes."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import context_pack, db
from core.memory import embeddings, semantic
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"X-Atlas-Client": "1", "Origin": "http://atlas"}


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async def fake_embed(text: str) -> list[float]:
        return [0.01] * 768

    monkeypatch.setattr(semantic, "embed", fake_embed)
    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr(context_pack, "load", lambda path=None: {"people": [
        {"name": "Brenda", "tier": 1, "org": "Example Property Group", "note": "rent report every Wednesday"},
        {"name": "Dr. Lee", "tier": 2, "project": "demo-dental"},
    ]})
    async with livedb.shadow(monkeypatch, "facts", "profile_notes"):
        yield


@aio
async def test_the_page_shows_profile_people_and_facts_and_takes_corrections(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        r = await c.get("/api/memory")
        assert r.status_code == 200
        assert r.json() == {"query": "", "total": 0, "facts": [], "profile": [],
                            "people": [{"name": "Brenda", "tier": 1, "org": "Example Property Group", "project": None, "note": "rent report every Wednesday"},
                                       {"name": "Dr. Lee", "tier": 2, "org": None, "project": "demo-dental", "note": None}]}

        r = await c.post("/api/memory/facts", json={"content": "The Kirkdale lender contact is Dan Roe."})
        assert r.status_code == 200 and r.json()["source"] == "owner"
        fid = r.json()["id"]
        r = await c.post("/api/memory/facts", json={"content": "Sidework's domain is sidework.ai."})
        assert r.status_code == 200
        assert (await c.post("/api/memory/facts", json={"content": "x"})).status_code == 422

        r = await c.get("/api/memory")
        assert r.json()["total"] == 2 and [f["content"][:8] for f in r.json()["facts"]] == ["Sidework", "The Kirk"]

        r = await c.patch(f"/api/memory/facts/{fid}", json={"content": "The Kirkdale lender contact is Dan Roe at First Bank."})
        assert r.status_code == 200 and r.json()["source"] == "owner/edited" and r.json()["confidence"] == 1.0
        assert (await db.fetchval("select content from facts where id = $1", fid)).endswith("First Bank.")
        assert (await c.patch(f"/api/memory/facts/{uuid4()}", json={"content": "nobody"})).status_code == 404

        r = await c.get("/api/memory", params={"q": "lender"})
        assert r.status_code == 200 and r.json()["query"] == "lender"
        assert r.json()["facts"][0]["id"] == fid          # the lexical half ranks the match first

        r = await c.delete(f"/api/memory/facts/{fid}")
        assert r.status_code == 200 and r.json()["forgotten"] == fid
        assert (await c.delete(f"/api/memory/facts/{fid}")).status_code == 404
        assert (await c.get("/api/memory")).json()["total"] == 1

        r = await c.post("/api/memory/profile", json={"content": "No new client work until Nov 15", "kind": "constraint", "expires_in_days": 30})
        assert r.status_code == 200 and r.json()["kind"] == "constraint" and r.json()["expires_at"]
        nid = r.json()["id"]
        r = await c.post("/api/memory/profile", json={"content": "Prefers short replies", "kind": "preference"})
        assert r.status_code == 200 and r.json()["expires_at"] is None
        assert (await c.post("/api/memory/profile", json={"content": "", "kind": "fact"})).status_code == 422
        assert (await c.post("/api/memory/profile", json={"content": "x", "kind": "vibe"})).status_code == 422
        prof = (await c.get("/api/memory")).json()["profile"]
        assert [n["kind"] for n in prof] == ["constraint", "preference"]
        assert (await c.delete(f"/api/memory/profile/{nid}")).status_code == 200
        assert (await c.delete(f"/api/memory/profile/{nid}")).status_code == 404
        assert [n["kind"] for n in (await c.get("/api/memory")).json()["profile"]] == ["preference"]


@aio
async def test_a_correction_lands_even_when_the_embedding_model_is_down(world, monkeypatch):
    fid = await semantic.add_fact("His HCC program ends December 10, 2026.", source="academic")

    async def down(text: str) -> list[float]:
        raise RuntimeError("ollama is down")
    monkeypatch.setattr(embeddings, "embed", down)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        r = await c.patch(f"/api/memory/facts/{fid}", json={"content": "His HCC program ends December 12, 2026."})
        assert r.status_code == 200 and r.json()["content"].endswith("12, 2026.") and r.json()["source"] == "owner/edited"
