"""Unit tests for the client_delivery agent."""

from __future__ import annotations

from agents.client_delivery import register_client_delivery
from agents.client_delivery.tools import build_tools as agent_build_tools
from core.runtime.dispatcher import get_registration


def test_register_client_delivery_wires_tools():
    register_client_delivery()
    reg = get_registration("client_delivery")
    assert reg is not None
    assert reg.model_tier == "specialist"
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "load_procedure" in names


def test_client_delivery_toolset():
    names = {t.name for t in agent_build_tools()}
    assert names == {
        "fetch_url",
        "load_procedure",
        "draft_email",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
    }
