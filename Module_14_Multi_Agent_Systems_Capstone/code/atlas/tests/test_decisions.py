"""Unit tests for the decision journal and lessons store.

Covers the data layers (core/decisions.py, core/lessons.py) and the CEO
tools that wrap them. Everything external is mocked: no Postgres, no
Ollama embeddings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agents.ceo.tools import build_tools
from core import decisions, lessons


def _decision_row(**over):
    base = {
        "id": uuid4(),
        "summary": "picked supplier A",
        "rationale": "best comms score",
        "expected_outcome": "samples in two weeks",
        "made_by": "owner",
        "domain": "side_shop",
        "status": "open",
        "outcome": None,
        "assessment": None,
        "decided_at": None,
        "review_at": None,
        "resolved_at": None,
        "goal_id": None,
        "episode_id": None,
        "metadata": {},
    }
    base.update(over)
    return base


def _lesson_row(**over):
    base = {
        "id": uuid4(),
        "content": "suppliers that score low on comms tend to ghost",
        "domain": "sourcing",
        "source_episode": None,
        "created_at": None,
        "score": 0.02,
        "matched": ["vector"],
    }
    base.update(over)
    return base


def _ceo_tool(name: str):
    return {t.name: t for t in build_tools()}[name].handler


# ---------------------------------------------------------------------------
# core/decisions.py
# ---------------------------------------------------------------------------


async def test_log_decision_returns_id():
    did = uuid4()
    with patch(
        "core.decisions.db.fetchrow", new=AsyncMock(return_value={"id": did})
    ) as fetchrow:
        result = await decisions.log_decision("ship it")
    assert result == did
    assert fetchrow.await_args.args[1] == "ship it"


async def test_list_decisions_open_filters():
    with patch(
        "core.decisions.db.fetch", new=AsyncMock(return_value=[_decision_row()])
    ) as fetch:
        out = await decisions.list_decisions()
    assert len(out) == 1
    assert fetch.await_args.args[1] == "open"


async def test_list_decisions_all_no_status_arg():
    with patch(
        "core.decisions.db.fetch", new=AsyncMock(return_value=[])
    ) as fetch:
        await decisions.list_decisions(status=None)
    assert len(fetch.await_args.args) == 1


async def test_find_decision_short_prefix_returns_none():
    assert await decisions.find_decision("abc") is None


async def test_find_decision_ambiguous_returns_none():
    with patch(
        "core.decisions.db.fetch",
        new=AsyncMock(return_value=[_decision_row(), _decision_row()]),
    ):
        assert await decisions.find_decision("abcd1234") is None


async def test_resolve_decision_reports_change():
    with patch(
        "core.decisions.db.execute", new=AsyncMock(return_value="UPDATE 1")
    ):
        assert await decisions.resolve_decision(uuid4(), "worked", "right") is True
    with patch(
        "core.decisions.db.execute", new=AsyncMock(return_value="UPDATE 0")
    ):
        assert await decisions.resolve_decision(uuid4(), "n/a", "right") is False


async def test_resolve_decision_rejects_bad_assessment():
    with pytest.raises(ValueError):
        await decisions.resolve_decision(uuid4(), "outcome", "great")


async def test_decisions_due_for_review_queries_open():
    with patch(
        "core.decisions.db.fetch", new=AsyncMock(return_value=[_decision_row()])
    ) as fetch:
        out = await decisions.decisions_due_for_review()
    assert len(out) == 1
    assert "status = 'open'" in fetch.await_args.args[0]


# ---------------------------------------------------------------------------
# core/lessons.py
# ---------------------------------------------------------------------------


async def test_record_lesson_embeds_and_returns_id():
    lid = uuid4()
    with (
        patch("core.lessons.embed", new=AsyncMock(return_value=[0.1, 0.2])),
        patch(
            "core.lessons.db.fetchrow", new=AsyncMock(return_value={"id": lid})
        ) as fetchrow,
    ):
        result = await lessons.record_lesson("low comms means ghosting")
    assert result == lid
    assert fetchrow.await_args.args[1] == "low comms means ghosting"


async def test_search_lessons_returns_shaped_rows():
    row = _lesson_row(score=0.031, matched=["vector", "lexical"])
    with patch("core.memory.retrieval.search", new=AsyncMock(return_value=[row])) as search:
        out = await lessons.search_lessons("supplier comms")
    search.assert_awaited_once_with("lessons", "supplier comms", limit=5)
    assert len(out) == 1
    assert out[0]["content"]
    assert out[0]["score"] == 0.031 and out[0]["matched"] == ["vector", "lexical"]


async def test_recent_lessons():
    with patch(
        "core.lessons.db.fetch", new=AsyncMock(return_value=[_lesson_row()])
    ):
        out = await lessons.recent_lessons(limit=5)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# CEO decision + lesson tools
# ---------------------------------------------------------------------------


def test_ceo_has_decision_and_lesson_tools():
    names = {t.name for t in build_tools()}
    assert {
        "log_decision",
        "resolve_decision",
        "record_lesson",
        "recall_lessons",
    } <= names


async def test_log_decision_tool():
    did = uuid4()
    with patch(
        "agents.ceo.tools.log_decision", new=AsyncMock(return_value=did)
    ) as log:
        out = await _ceo_tool("log_decision")(
            {"summary": "picked supplier A", "rationale": "best comms"}
        )
    assert out["decision_id"] == str(did)
    assert log.await_args.kwargs["summary"] == "picked supplier A"


async def test_log_decision_tool_rejects_bad_review_date():
    out = await _ceo_tool("log_decision")(
        {"summary": "x", "review_at": "soon"}
    )
    assert "error" in out


async def test_log_decision_tool_requires_summary():
    out = await _ceo_tool("log_decision")({"summary": "  "})
    assert "error" in out


async def test_resolve_decision_tool():
    row = _decision_row()
    with (
        patch(
            "agents.ceo.tools.find_decision", new=AsyncMock(return_value=row)
        ),
        patch(
            "agents.ceo.tools.resolve_decision",
            new=AsyncMock(return_value=True),
        ) as resolve,
    ):
        out = await _ceo_tool("resolve_decision")(
            {
                "decision_id": "abcd1234",
                "outcome": "samples arrived late",
                "assessment": "mixed",
            }
        )
    assert out["assessment"] == "mixed"
    assert out["changed"] is True
    resolve.assert_awaited_once()


async def test_resolve_decision_tool_rejects_bad_assessment():
    row = _decision_row()
    with patch(
        "agents.ceo.tools.find_decision", new=AsyncMock(return_value=row)
    ):
        out = await _ceo_tool("resolve_decision")(
            {"decision_id": "abcd1234", "outcome": "x", "assessment": "good"}
        )
    assert "error" in out


async def test_resolve_decision_tool_unknown_decision():
    with patch(
        "agents.ceo.tools.find_decision", new=AsyncMock(return_value=None)
    ):
        out = await _ceo_tool("resolve_decision")(
            {"decision_id": "ffffffff", "outcome": "x", "assessment": "right"}
        )
    assert "error" in out


async def test_record_lesson_tool():
    lid = uuid4()
    with patch(
        "agents.ceo.tools.record_lesson", new=AsyncMock(return_value=lid)
    ):
        out = await _ceo_tool("record_lesson")(
            {"content": "verify certs before paying a deposit"}
        )
    assert out["lesson_id"] == str(lid)


async def test_record_lesson_tool_requires_content():
    out = await _ceo_tool("record_lesson")({"content": ""})
    assert "error" in out


async def test_recall_lessons_tool():
    with patch(
        "core.runtime.common_tools.search_lessons",
        new=AsyncMock(return_value=[_lesson_row(), _lesson_row()]),
    ):
        out = await _ceo_tool("recall_lessons")({"query": "supplier risk"})
    assert out["count"] == 2
    assert len(out["lessons"]) == 2
