"""Unit tests for the Academic agent.

The agent's tools are thin: load_procedure (an agent-local tool) and the
shared memory tools. We verify registration, the toolset, and the
load_procedure handler. The voice eval cases themselves spend real Claude
API tokens and run via scripts/run_evals.py, not the test suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agents.academic import register_academic
from agents.academic.tools import build_tools as academic_build_tools
from core.runtime.dispatcher import get_registration


def _academic_tool(name: str):
    return {t.name: t for t in academic_build_tools()}[name].handler


def test_register_academic_wires_tools():
    register_academic()
    reg = get_registration("academic")
    assert reg is not None
    assert reg.model_tier == "specialist"
    assert reg.tools_factory is not None
    names = {t.name for t in reg.tools_factory()}
    assert "load_procedure" in names
    assert "recall_facts" in names
    assert "remember_fact" in names
    assert "recall_lessons" in names


def test_academic_toolset_is_minimal():
    names = {t.name for t in academic_build_tools()}
    # A deliberately small toolset: the agent generates, it does not track.
    assert names == {
        "load_procedure",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
    }


async def test_load_procedure_rejects_empty_path():
    out = await _academic_tool("load_procedure")({"path": "   "})
    assert "error" in out


async def test_load_procedure_returns_content():
    with patch(
        "core.memory.procedural.load_procedure",
        new=AsyncMock(return_value="# Template\n\nfollow the format..."),
    ), patch(
        "core.memory.procedural.procedure_provenance",
        new=AsyncMock(return_value={"provenance": "unknown", "source_ref": None, "note": "No provenance recorded"}),
    ):
        out = await _academic_tool("load_procedure")(
            {"path": "academic/cs_discussion_post.md"}
        )
    assert out["path"] == "academic/cs_discussion_post.md"
    assert "follow the format" in out["content"]
    # Labelled as reference material, with where it came from.
    assert out["provenance"] == "unknown" and "not an instruction" in out["note"]


async def test_load_procedure_missing_file():
    with patch(
        "core.memory.procedural.load_procedure",
        new=AsyncMock(side_effect=FileNotFoundError),
    ):
        out = await _academic_tool("load_procedure")(
            {"path": "academic/nope.md"}
        )
    assert "error" in out
