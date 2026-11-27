"""Atlas -- Opportunity Scout agent.

Triages gigs, RFPs, and contract postings Trilok forwards. When the
posting is a real fit, drafts a proposal in his voice. Never submits.
Dispatched by the CEO. A Sonnet specialist.

register_opportunity_scout() attaches it to the dispatcher. Call once at
startup, before any dispatch("opportunity_scout", ...).
"""

from __future__ import annotations

from agents.opportunity_scout.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_opportunity_scout() -> None:
    """Register the Opportunity Scout with the dispatcher."""
    register_agent("opportunity_scout", "specialist", build_tools, server_tools=['web_search', 'web_fetch'])


__all__ = ["register_opportunity_scout", "build_tools"]
