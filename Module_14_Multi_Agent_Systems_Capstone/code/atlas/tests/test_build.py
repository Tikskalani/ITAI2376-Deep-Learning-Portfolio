"""Unit tests for the build agent."""

from __future__ import annotations

from agents.build import register_build
from agents.build.tools import build_tools as agent_build_tools
from core.runtime.dispatcher import get_registration


def test_register_build_wires_tools():
    register_build()
    reg = get_registration("build")
    assert reg is not None
    assert reg.model_tier == "specialist"
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "load_procedure" in names


def test_build_toolset():
    names = {t.name for t in agent_build_tools()}
    assert names == {
        "fetch_url",
        "load_procedure",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
    }
