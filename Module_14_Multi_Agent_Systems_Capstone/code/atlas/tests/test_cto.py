"""Unit tests for the cto agent."""

from __future__ import annotations

from agents.cto import register_cto
from agents.cto.tools import build_tools as agent_build_tools
from core.runtime.dispatcher import get_registration


def test_register_cto_wires_tools():
    register_cto()
    reg = get_registration("cto")
    assert reg is not None
    assert reg.model_tier == "specialist"
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "load_procedure" in names


def test_cto_toolset():
    names = {t.name for t in agent_build_tools()}
    assert names == {
        "fetch_url",
        "load_procedure",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
        # Approved procedures from shared tutorials (core/capabilities/), while the feature is on.
        "search_capabilities",
        "get_capability",
        "report_capability_outcome",
    }
