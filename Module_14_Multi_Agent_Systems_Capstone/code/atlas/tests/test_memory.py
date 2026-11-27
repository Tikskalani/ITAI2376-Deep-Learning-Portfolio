"""End-to-end smoke test for the Atlas memory API.

Exercises semantic (entity, attribute, relationship, fact + vector search),
episodic (start/end/search), and prospective (create + due) layers against
a live Postgres+pgvector and Ollama. Procedural sync is not in this smoke
test because it touches the on-disk procedures/ tree and we don't want
the test creating fixtures there.

Run from the repo root:

    pytest tests/test_memory.py -v

Requires:
    docker compose up -d            # postgres + ollama running
    ollama pull nomic-embed-text    # one time
    psql -f db/migrations/001_initial.sql atlas   # schema applied
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from core import db
from core.memory import (
    add_attribute,
    add_entity,
    add_fact,
    add_relationship,
    cancel_trigger,
    create_trigger,
    end_episode,
    find_entities_by_name,
    get_due_triggers,
    get_entity,
    search_episodes,
    search_facts,
    start_episode,
)
from tests import livedb

# Use one event loop and one asyncpg pool for the whole module so the
# pool isn't bound to a closed loop between tests.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private_facts(monkeypatch):
    # Real facts about Trilok would compete with the test's in the ranking,
    # and every search bumps the last_accessed of what it returns.
    async with livedb.shadow(monkeypatch, "facts"):
        yield


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cleanup_tag():
    """A unique marker we stamp on every row this run, so teardown is surgical."""
    tag = f"test-memory-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    yield tag
    # Best-effort cleanup. Don't fail the test on cleanup errors.
    try:
        await db.execute("delete from triggers where description like $1", f"%{tag}%")
        await db.execute("delete from facts where source = $1", tag)
        await db.execute("delete from episodes where $1 = any(tags)", tag)
        await db.execute(
            "delete from entities where metadata->>'test_tag' = $1",
            tag,
        )
    except Exception:
        pass
    finally:
        await db.close_pool()


async def test_semantic_entity_attribute_relationship_fact(cleanup_tag):
    """Add an entity, attribute, relationship, and embedded fact. Then search."""
    tag = cleanup_tag

    trilok_id = await add_entity(
        type="person",
        name="Trilok",
        canonical_name="trilok",
        metadata={"test_tag": tag},
    )
    partner_id = await add_entity(
        type="person",
        name="Jordan",
        canonical_name="jordan",
        metadata={"test_tag": tag},
    )
    assert trilok_id is not None
    assert partner_id is not None

    attr_id = await add_attribute(
        trilok_id,
        key="role",
        value="software engineer, AI/ML focus",
        source=tag,
    )
    assert attr_id is not None

    rel_id = await add_relationship(
        trilok_id,
        partner_id,
        rel_type="married_to",
        metadata={"since": "long-time"},
    )
    assert rel_id is not None

    profile = await get_entity(trilok_id)
    assert profile is not None
    assert profile["name"] == "Trilok"
    assert any(a["key"] == "role" for a in profile["attributes"])
    assert any(r["rel_type"] == "married_to" for r in profile["relationships"]["outgoing"])

    found = await find_entities_by_name("TRILOK")
    assert any(e["id"] == trilok_id for e in found)

    fact_id = await add_fact(
        "Trilok is studying artificial intelligence and machine learning at HCC.",
        entity_ids=[trilok_id],
        source=tag,
    )
    assert fact_id is not None

    hits = await search_facts("Who is studying AI in this household?", limit=5)
    assert hits, "search_facts returned no results"
    top = hits[0]
    assert top["score"] > 0 and top["matched"]
    assert any(h["id"] == fact_id for h in hits[:5]), (
        f"expected fact {fact_id} in top 5, got {[h['id'] for h in hits]}"
    )


async def test_episodic_start_end_search(cleanup_tag):
    """Open an episode, close it with a summary, then find it via vector search."""
    tag = cleanup_tag
    ep_id = await start_episode(
        agent_name="ceo",
        participants=["owner", "ceo"],
    )
    assert ep_id is not None

    await end_episode(
        ep_id,
        summary=(
            "Owner asked the CEO agent to outline the Side Shop sourcing "
            "strategy for the next quarter. CEO drafted a three-supplier "
            "shortlist on Alibaba and queued the Sourcing agent to evaluate."
        ),
        outcome="dispatched sourcing agent with shortlist",
        what_changed={"goal": "side-shop-q3", "dispatched_to": "sourcing"},
        tags=[tag, "side-shop", "sourcing"],
    )

    results = await search_episodes(
        "How did we decide on Alibaba suppliers for Side Shop?",
        limit=3,
    )
    assert results, "search_episodes returned no results"
    ids = [r["id"] for r in results]
    assert ep_id in ids, f"expected episode {ep_id} in top 3, got {ids}"
    top = next(r for r in results if r["id"] == ep_id)
    assert top["agent_name"] == "ceo"
    assert "side-shop" in top["tags"]


async def test_prospective_trigger_due_after_two_seconds(cleanup_tag):
    """Schedule a trigger 2s ahead, wait, confirm it's reported as due."""
    tag = cleanup_tag
    fires_at = datetime.now(UTC) + timedelta(seconds=2)

    trigger_id = await create_trigger(
        trigger_type="time",
        spec={"once": True},
        payload={"agent": "coo", "task": "remind", "message": "2-second test ping"},
        description=f"smoke test trigger {tag}",
        fires_at=fires_at,
    )
    assert trigger_id is not None

    not_yet_due = await get_due_triggers()
    assert not any(t["id"] == trigger_id for t in not_yet_due), (
        "trigger reported as due before fires_at"
    )

    await asyncio.sleep(2.5)
    due = await get_due_triggers()
    assert any(t["id"] == trigger_id for t in due), (
        f"trigger {trigger_id} not in due list after fires_at"
    )

    await cancel_trigger(trigger_id)
