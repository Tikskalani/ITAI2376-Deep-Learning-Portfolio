"""Tests for memory v2: hybrid retrieval, the profile, consolidation, and
the MCP server's tools and HTTP mount. Embeddings are written directly as
vectors (the cloud VM has no embedding model) and llm.complete is faked;
the database is the live test one."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from core import db, profile, projects
from core.memory import consolidation, retrieval
from core.memory.embeddings import EmbeddingError
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _vec(seed: int, *, dim: int = 768, noise: float = 0.0) -> list[float]:
    rnd = random.Random(seed)
    base = [rnd.uniform(-1, 1) for _ in range(dim)]
    if noise:
        base = [x + rnd.uniform(-noise, noise) for x in base]
    norm = sum(x * x for x in base) ** 0.5
    return [x / norm for x in base]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Consolidation dedupes and decays every fact, expires every note and writes
    # its heartbeat; the MCP tools add facts, notes and deadlines under shared
    # sources. Real ones stay out of reach. Episodes stay real (decisions point
    # at them), so only the ones these tests wrote are removed.
    async with livedb.shadow(monkeypatch, "facts", "profile_notes", "deadlines", "system_heartbeats"):
        yield
        await db.execute("delete from lessons where domain = 'test-v2'")
        await db.execute(
            "delete from episodes where agent_name = 'test-v2' or (agent_name = 'claude/session'"
            " and exists (select 1 from unnest(tags) t where t like 'test-%'))"
        )
        await db.execute("delete from decisions where domain like 'test-%'")
        await db.execute("delete from projects where slug like 'test-%'")


async def _fact(content: str, seed: int, *, source: str = "test-v2", noise: float = 0.0,
                last_accessed_days_ago: int = 0, confidence: float = 1.0):
    return await db.fetchval(
        """
        insert into facts (content, embedding, source, confidence, last_accessed)
        values ($1, $2, $3, $4, now() - ($5 * interval '1 day')) returning id
        """,
        content, _vec(seed, noise=noise), source, confidence, last_accessed_days_ago,
    )


async def test_rrf_pure():
    fused = retrieval.rrf(["a", "b", "c"], ["b", "d"], [])
    assert [i for i, _ in fused][:2] == ["b", "a"]
    assert retrieval.rrf([], []) == []


async def test_hybrid_search_fuses_lexical_and_vector_and_degrades_without_embeddings(monkeypatch):
    inv = await _fact("Demo Dental invoice #4471 is due October 20", 1)
    await _fact("Chris prefers Zoom over phone calls", 2)
    await _fact("The HVAC site deploys from the main branch", 3)

    # Lexical only: the embedding service is down, the exact token still finds it.
    async def down(_q):
        raise EmbeddingError("ollama offline")

    monkeypatch.setattr(retrieval, "embed", down)
    rows = await retrieval.search("facts", "invoice 4471", limit=3)
    assert rows and rows[0]["id"] == inv and rows[0]["matched"] == ["lexical"]

    # Vector leg: a query embedding near fact 2 surfaces it even with no shared words.
    async def near_two(_q):
        return _vec(2, noise=0.05)

    monkeypatch.setattr(retrieval, "embed", near_two)
    rows = await retrieval.search("facts", "zzz no overlap", limit=3)
    assert rows and "Zoom" in rows[0]["content"] and "vector" in rows[0]["matched"]

    # Both legs agree: the fused score tops the list.
    rows = await retrieval.search("facts", "Zoom calls", limit=3)
    assert "Zoom" in rows[0]["content"] and set(rows[0]["matched"]) >= {"vector", "lexical"}
    assert await retrieval.search("facts", "   ", limit=3) == []
    with pytest.raises(ValueError):
        await retrieval.search("nope", "x")


async def test_search_facts_keeps_shape_and_bumps_last_accessed(monkeypatch):
    from core.memory import semantic

    fid = await _fact("Sidework pilot kickoff is November 3", 7, last_accessed_days_ago=10)

    async def down(_q):
        raise EmbeddingError("offline")

    monkeypatch.setattr(retrieval, "embed", down)
    rows = await semantic.search_facts("Sidework kickoff", limit=2)
    assert rows[0]["id"] == fid and rows[0]["content"].startswith("Sidework")
    assert {"id", "content", "source", "confidence", "score", "matched"} <= set(rows[0])
    bumped = await db.fetchval("select last_accessed from facts where id = $1", fid)
    assert (datetime.now(UTC) - bumped).total_seconds() < 60


async def test_profile_notes_block_expiry_and_validation():
    a = await profile.set_note("Trilok writes in plain prose, no em-dashes", kind="preference", source="test-p")
    b = await profile.set_note("No new client work until the move is done", kind="constraint", expires_in_days=10, source="test-p")
    c = await profile.set_note("already over", kind="constraint", expires_in_days=1, source="test-p")
    await db.execute("update profile_notes set expires_at = now() - interval '1 day' where id = $1", c)
    block = await profile.profile_block()
    assert "- Preference: Trilok writes in plain prose" in block
    assert "- Constraint: No new client work until the move is done (until " in block
    assert "already over" not in block
    assert await profile.expire_notes() >= 1
    assert c not in {n["id"] for n in await profile.list_notes(include_expired=True)}
    with pytest.raises(ValueError):
        await profile.set_note("x", kind="rumour")
    with pytest.raises(ValueError):
        await profile.set_note("y" * 400)
    assert await profile.remove_note(a) and await profile.remove_note(b)
    assert profile.format_block([]) == ""


async def test_consolidation_dedupes_decays_and_writes_context_packs(monkeypatch):
    keep = await _fact("Demo Dental pays net 30", 11, source="test-dup-a")
    dup = await _fact("Demo Dental pays net 30.", 11, source="test-dup-b", noise=0.001)
    far = await _fact("Something unrelated", 12, source="test-far")
    stale = await _fact("Old fact nobody reads", 13, source="test-stale", last_accessed_days_ago=120)

    removed = await consolidation.dedupe_facts()
    assert removed >= 1
    assert await db.fetchval("select count(*) from facts where id = $1", dup) == 0
    kept = await db.fetchrow("select source, confidence from facts where id = $1", keep)
    assert kept is not None and "+dup(" in kept["source"]
    assert await db.fetchval("select count(*) from facts where id = $1", far) == 1

    decayed = await consolidation.decay_stale_facts()
    assert decayed >= 1
    conf = await db.fetchval("select confidence from facts where id = $1", stale)
    assert 0.89 < conf < 0.91

    p = await projects.upsert_project("test-pack", "Pack Project", objective="Ship it")

    async def fake_complete(**kw):
        assert kw["purpose"] == "context_pack" and "Pack Project" in kw["user"]
        return "Pack Project is shipping. Nothing waits on anyone."

    async def no_search(table, query, **kw):
        return []

    monkeypatch.setattr(consolidation.llm, "complete", fake_complete)
    monkeypatch.setattr(consolidation.retrieval, "search", no_search)
    assert await consolidation.regenerate_context_packs(projects=[p]) == 1
    row = await db.fetchrow("select context_pack, context_pack_at from projects where id = $1", p.id)
    assert row is not None and row["context_pack"].startswith("Pack Project is shipping") and row["context_pack_at"]

    with patch("core.memory.consolidation.list_projects", new=AsyncMock(return_value=[])):
        out = await consolidation.run_consolidation()
    assert set(out) == {"duplicates_removed", "facts_decayed", "notes_expired", "packs_written"}


async def test_mcp_tools_read_and_write_back(monkeypatch):
    from core import mcp_server as m

    p = await projects.upsert_project("test-mcp", "MCP Project", priority=1, objective="Prove it")
    await db.execute("update projects set context_pack = 'the pack' where id = $1", p.id)

    listed = await m.list_projects()
    assert any(x["slug"] == "test-mcp" and x["priority"] == 1 for x in listed)
    ctx = await m.get_project_context("test-mcp")
    assert ctx["context_pack"] == "the pack" and ctx["objective"] == "Prove it"
    assert (await m.get_project_context("nope"))["error"]

    async def fake_embed(_text):
        return _vec(99)

    monkeypatch.setattr("core.memory.semantic.embed", fake_embed)
    monkeypatch.setattr("core.memory.episodic.embed", fake_embed)
    out = await m.record_outcome("test-mcp", "Reviewed the portal with Chris.",
                                 next_action="Send the invoice", next_action_due="2026-10-20",
                                 decisions=["Use Stripe for client billing"])
    assert "episode_id" in out and out["next_action"] == "Send the invoice" and len(out["decisions"]) == 1
    again = await projects.get_project("test-mcp")
    assert again is not None and again.next_action == "Send the invoice"
    ep = await db.fetchrow("select project_id, summary from episodes where id = $1", out["episode_id"])
    assert ep is not None and ep["project_id"] == p.id
    assert (await m.record_outcome("test-mcp", "x", next_action="y", next_action_due="soon"))["error"]

    fact = await m.remember_fact("MCP Project bills on the 1st", project="test-mcp")
    assert "fact_id" in fact
    await db.execute("update facts set source = 'test-mcp-fact' where id = $1", fact["fact_id"])
    dl = await m.add_deadline("Invoice", "2026-10-20", project="test-mcp", priority="high")
    assert "deadline_id" in dl
    assert (await m.add_deadline("x", "not-a-date"))["error"]
    ctx = await m.get_project_context("test-mcp")
    assert ctx["open_deadlines"][0]["title"] == "Invoice"
    note = await m.set_profile_note("Prefers morning meetings", kind="preference")
    assert "note_id" in note
    assert (await m.set_profile_note("x", kind="rumour"))["error"]
    await db.execute("delete from deadlines where id = $1", dl["deadline_id"])


async def test_mcp_http_mount_requires_token(monkeypatch):
    from core.web.api import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as client:
        monkeypatch.setattr("core.web.api.settings.mcp_token", "")
        r = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 503
        monkeypatch.setattr("core.web.api.settings.mcp_token", "s3cret")
        r = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        r = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


async def test_scheduler_has_consolidation():
    from core.loop.scheduler import build_scheduler

    ids = {j.id for j in build_scheduler().get_jobs()}
    assert {"memory_consolidation", "workflow_tick"} <= ids
    assert timedelta(days=1)  # keep the import honest
