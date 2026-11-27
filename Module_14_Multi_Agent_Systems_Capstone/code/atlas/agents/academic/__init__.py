"""Atlas -- Academic agent.

Drafts HCC coursework in Trilok's voice: discussion posts, assignment
responses, study guides. Dispatched by the CEO. A Sonnet specialist.
Voice discipline is strict; the agent never submits, only drafts.

register_academic() attaches it to the dispatcher, wiring in its tools.
Call it once at startup, before any dispatch("academic", ...).
"""

from __future__ import annotations

from agents.academic.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_academic() -> None:
    """Register the Academic agent with the dispatcher, with its tools."""
    register_agent("academic", "specialist", build_tools)


__all__ = ["register_academic", "build_tools"]
