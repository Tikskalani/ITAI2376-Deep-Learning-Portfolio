"""Atlas -- Opportunity Scout agent tools.

A light toolset. The agent fetches gig postings, triages, and drafts
proposals in Trilok's voice. Distillation lives in the agent. The only
persistent side effects are memory writes through the shared tools.

build_tools() returns the ToolSpec list the runtime hands to the model.
"""

from __future__ import annotations

from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    record_lesson_tool,
    remember_fact_tool,
)
from core.web import fetch_url_tool


def build_tools() -> list[ToolSpec]:
    """Return the Opportunity Scout's toolset."""
    return [
        fetch_url_tool(
            "Use it on any gig posting URL Trilok forwards before you triage."
        ),
        recall_facts_tool(),
        remember_fact_tool("opportunity_scout"),
        recall_lessons_tool(),
        record_lesson_tool(domain="opportunity_scout"),
    ]


__all__ = ["build_tools"]
