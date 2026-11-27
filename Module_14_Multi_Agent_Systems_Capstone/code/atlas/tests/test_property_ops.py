"""Unit tests for the Property Ops agent."""

from __future__ import annotations

from agents.property_ops import register_property_ops
from agents.property_ops.tools import build_tools as po_build_tools
from core.runtime.dispatcher import get_registration


def test_register_property_ops_wires_tools():
    register_property_ops()
    reg = get_registration("property_ops")
    assert reg is not None
    assert reg.model_tier == "specialist"
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "load_procedure" in names


def test_property_ops_toolset():
    names = {t.name for t in po_build_tools()}
    assert names == {
        "fetch_url",
        "load_procedure",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
    }
