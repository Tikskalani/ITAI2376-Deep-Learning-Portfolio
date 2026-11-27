"""Unit tests for the Opportunity Scout agent.

The agent shares fetch_url with News & Discovery via core.web; that
fetcher already has its own tests. Here we cover registration, the
toolset, and that the agent's fetch_url description carries its own
gig-posting framing so the model is steered right.
"""

from __future__ import annotations

from agents.opportunity_scout import register_opportunity_scout
from agents.opportunity_scout.tools import build_tools as os_build_tools
from core.runtime.dispatcher import get_registration


def _os_tool(name: str):
    return {t.name: t for t in os_build_tools()}[name].handler


def test_register_opportunity_scout_wires_tools():
    register_opportunity_scout()
    reg = get_registration("opportunity_scout")
    assert reg is not None
    assert reg.model_tier == "specialist"
    assert reg.tools_factory is not None
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "record_lesson" in names


def test_opportunity_scout_toolset():
    names = {t.name for t in os_build_tools()}
    assert names == {
        "fetch_url",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
    }


def test_fetch_url_carries_gig_posting_framing():
    """The agent's fetch_url description must mention gig postings so the
    model frames the page right when triaging."""
    tools = {t.name: t for t in os_build_tools()}
    desc = tools["fetch_url"].description
    assert "gig posting" in desc.lower()
