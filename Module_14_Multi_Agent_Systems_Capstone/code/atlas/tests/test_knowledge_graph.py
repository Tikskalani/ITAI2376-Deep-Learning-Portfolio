"""Unit tests for core/knowledge_graph.py.

Anthropic, Postgres, and the running event loop are all mocked. No real
Haiku call, no DB.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from core import knowledge_graph as kg

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_clean_json():
    raw = json.dumps(
        {
            "entities": [
                {
                    "type": "business",
                    "name": "Example Property Group",
                    "canonical_name": "example property group",
                },
                {
                    "type": "person",
                    "name": "Trilok",
                    "canonical_name": "trilok",
                },
            ],
            "relationships": [
                {
                    "from": "trilok",
                    "to": "example property group",
                    "rel_type": "employs",
                }
            ],
        }
    )
    out = kg._parse_extraction(raw)
    assert len(out["entities"]) == 2
    assert len(out["relationships"]) == 1
    assert out["relationships"][0]["rel_type"] == "employs"


def test_parse_strips_markdown_fences():
    raw = '```json\n{"entities": [], "relationships": []}\n```'
    out = kg._parse_extraction(raw)
    assert out == {"entities": [], "relationships": []}


def test_parse_invalid_json_returns_empty():
    out = kg._parse_extraction("not actually json")
    assert out == {"entities": [], "relationships": []}


def test_parse_filters_disallowed_entity_types():
    """A type not in ALLOWED_TYPES gets dropped."""
    raw = json.dumps(
        {
            "entities": [
                {"type": "magic", "name": "X", "canonical_name": "x"},
                {"type": "person", "name": "Y", "canonical_name": "y"},
            ],
            "relationships": [],
        }
    )
    out = kg._parse_extraction(raw)
    assert len(out["entities"]) == 1
    assert out["entities"][0]["name"] == "Y"


def test_parse_dedupes_canonicals_within_extraction():
    raw = json.dumps(
        {
            "entities": [
                {"type": "business", "name": "Side Shop", "canonical_name": "side shop"},
                {"type": "business", "name": "side shop", "canonical_name": "side shop"},
            ],
            "relationships": [],
        }
    )
    out = kg._parse_extraction(raw)
    assert len(out["entities"]) == 1


def test_parse_drops_relationships_with_unknown_endpoints():
    raw = json.dumps(
        {
            "entities": [
                {"type": "person", "name": "A", "canonical_name": "a"}
            ],
            "relationships": [
                {"from": "a", "to": "ghost", "rel_type": "knows"}
            ],
        }
    )
    out = kg._parse_extraction(raw)
    assert out["relationships"] == []


def test_parse_drops_self_loops():
    raw = json.dumps(
        {
            "entities": [
                {"type": "person", "name": "A", "canonical_name": "a"}
            ],
            "relationships": [
                {"from": "a", "to": "a", "rel_type": "thinks_about"}
            ],
        }
    )
    out = kg._parse_extraction(raw)
    assert out["relationships"] == []


# ---------------------------------------------------------------------------
# upsert_entity
# ---------------------------------------------------------------------------


async def test_upsert_entity_returns_existing_id_when_canonical_matches_same_type():
    existing_id = uuid4()
    with patch(
        "core.knowledge_graph.db.fetchrow",
        new=AsyncMock(
            return_value={
                "id": existing_id,
                "type": "business",
                "metadata": {},
            }
        ),
    ) as fetchrow:
        out = await kg.upsert_entity("business", "Example Property Group")
    assert out == existing_id
    assert fetchrow.await_count == 1


async def test_upsert_entity_inserts_when_no_match():
    new_id = uuid4()
    fetchrow = AsyncMock(side_effect=[None, {"id": new_id}])
    with patch("core.knowledge_graph.db.fetchrow", new=fetchrow):
        out = await kg.upsert_entity(
            "business", "Side Shop", canonical_name="side shop"
        )
    assert out == new_id
    assert fetchrow.await_count == 2


async def test_upsert_entity_dedupes_across_types_and_tracks_aliases():
    """Same canonical, different type -> reuse the existing row but record
    the type evolution under metadata.types."""
    existing_id = uuid4()
    fetchrow = AsyncMock(
        return_value={
            "id": existing_id,
            "type": "business",
            "metadata": {},
        }
    )
    execute = AsyncMock()
    with (
        patch("core.knowledge_graph.db.fetchrow", new=fetchrow),
        patch("core.knowledge_graph.db.execute", new=execute),
    ):
        out = await kg.upsert_entity("product", "Side Shop")
    assert out == existing_id
    # The update path was taken; metadata.types now lists both.
    execute.assert_awaited_once()
    meta_arg = execute.await_args.args[2]
    assert set(meta_arg["types"]) == {"business", "product"}


async def test_upsert_entity_preserves_existing_alias_history():
    """If metadata.types already has values, the new type is merged in."""
    existing_id = uuid4()
    fetchrow = AsyncMock(
        return_value={
            "id": existing_id,
            "type": "business",
            "metadata": {"types": ["business", "product"]},
        }
    )
    execute = AsyncMock()
    with (
        patch("core.knowledge_graph.db.fetchrow", new=fetchrow),
        patch("core.knowledge_graph.db.execute", new=execute),
    ):
        await kg.upsert_entity("brand", "Side Shop")
    meta_arg = execute.await_args.args[2]
    assert set(meta_arg["types"]) == {"business", "product", "brand"}


# ---------------------------------------------------------------------------
# link_text orchestrator
# ---------------------------------------------------------------------------


async def test_link_text_skips_short_text():
    """Below MIN_TEXT_CHARS the orchestrator no-ops without calling Haiku."""
    with patch(
        "core.knowledge_graph.extract_from_text", new=AsyncMock()
    ) as extract:
        result = await kg.link_text("hi")
    assert "skipped" in result
    extract.assert_not_awaited()


async def test_link_text_no_entities_returns_zero_counts():
    with patch(
        "core.knowledge_graph.extract_from_text",
        new=AsyncMock(return_value={"entities": [], "relationships": []}),
    ):
        result = await kg.link_text("a" * 100)
    assert result["entities_added"] == 0
    assert result["relationships_added"] == 0


async def test_link_text_upserts_entities_and_inserts_relationships():
    ent_id_a = uuid4()
    ent_id_b = uuid4()
    rel_id = uuid4()
    with (
        patch(
            "core.knowledge_graph.extract_from_text",
            new=AsyncMock(
                return_value={
                    "entities": [
                        {"type": "business", "name": "Oakline", "canonical_name": "oakline"},
                        {"type": "person", "name": "Trilok", "canonical_name": "trilok"},
                    ],
                    "relationships": [
                        {"from": "trilok", "to": "oakline", "rel_type": "employs"}
                    ],
                }
            ),
        ),
        patch(
            "core.knowledge_graph.upsert_entity",
            new=AsyncMock(side_effect=[ent_id_a, ent_id_b]),
        ) as upsert,
        patch(
            "core.knowledge_graph.add_relationship",
            new=AsyncMock(return_value=rel_id),
        ) as add_rel,
    ):
        result = await kg.link_text("a" * 100, source="episode-123")
    assert result["entities_added"] == 2
    assert result["relationships_added"] == 1
    assert upsert.await_count == 2
    add_rel.assert_awaited_once()
    # Relationship should be tagged with the source.
    assert (
        add_rel.await_args.kwargs["metadata"]["source"] == "episode-123"
    )


async def test_link_text_survives_extractor_failure():
    """Haiku exception -> empty payload, no DB calls, no raise."""
    with (
        patch(
            "core.knowledge_graph._haiku_extract",
            new=AsyncMock(side_effect=RuntimeError("api down")),
        ),
        patch("core.knowledge_graph.upsert_entity", new=AsyncMock()) as upsert,
        patch(
            "core.knowledge_graph.add_relationship", new=AsyncMock()
        ) as add_rel,
    ):
        result = await kg.link_text("a" * 100)
    assert result["entities_added"] == 0
    upsert.assert_not_awaited()
    add_rel.assert_not_awaited()


async def test_link_text_continues_when_one_entity_fails():
    ent_id_a = uuid4()
    with (
        patch(
            "core.knowledge_graph.extract_from_text",
            new=AsyncMock(
                return_value={
                    "entities": [
                        {"type": "business", "name": "A", "canonical_name": "a"},
                        {"type": "business", "name": "B", "canonical_name": "b"},
                    ],
                    "relationships": [],
                }
            ),
        ),
        patch(
            "core.knowledge_graph.upsert_entity",
            new=AsyncMock(side_effect=[ent_id_a, RuntimeError("constraint")]),
        ),
    ):
        result = await kg.link_text("a" * 100)
    assert result["entities_added"] == 1


# ---------------------------------------------------------------------------
# Fire-and-forget scheduling
# ---------------------------------------------------------------------------


async def test_fire_and_forget_schedules_a_task():
    """Schedules link_text on the running loop; returns synchronously."""
    completed = asyncio.Event()

    async def fake_link(text, *, source=None):
        completed.set()

    with patch("core.knowledge_graph.link_text", new=fake_link):
        kg.link_text_fire_and_forget("a" * 100, source="test")
        # Yield control so the scheduled task can run.
        await asyncio.wait_for(completed.wait(), timeout=1.0)
    assert completed.is_set()


def test_fire_and_forget_without_loop_silently_drops():
    """No running loop -> early return, no exception."""
    # We're at module/synchronous level here (no asyncio.run() wrapping).
    kg.link_text_fire_and_forget("a" * 100)


# ---------------------------------------------------------------------------
# run_agent CEO hook
# ---------------------------------------------------------------------------


def test_run_agent_imports_link_text_fire_and_forget():
    """Smoke test: the source string ties run_agent to the hook."""
    import core.runtime.agent as agent_mod

    src = open(agent_mod.__file__).read()
    assert "link_text_fire_and_forget" in src
    assert 'config.name == "ceo"' in src


async def test_extraction_goes_through_core_llm_not_raw_sdk_kwargs():
    """Regression: the extractor passed temperature= straight to the 1.x SDK,
    whose create() no longer takes it, so every extraction raised TypeError
    and was swallowed. It must go through core.llm.complete, which shapes the
    request per model and records the call against the daily cap."""
    from unittest.mock import AsyncMock, patch

    from core import knowledge_graph

    fake = AsyncMock(return_value='{"entities": [], "relationships": []}')
    with patch("core.llm.complete", new=fake):
        out = await knowledge_graph._haiku_extract("Trilok met Dr. Pat Lee.")
    assert out.startswith("{")
    kwargs = fake.await_args.kwargs
    assert kwargs["tier"] == "scan" and kwargs["purpose"] == "knowledge_graph"
