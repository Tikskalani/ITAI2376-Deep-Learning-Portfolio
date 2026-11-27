"""Atlas -- COO agent.

Owns operations: tracks deadlines to completion, keeps recurring task
templates, runs the slipping check that catches anything overdue or due
soon. Dispatched by the CEO. A Sonnet specialist.

register_coo() attaches it to the dispatcher, wiring in its tools. Call
it once at startup, before any dispatch("coo", ...).
"""

from __future__ import annotations

from agents.coo.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_coo() -> None:
    """Register the COO agent with the dispatcher, with its tools."""
    register_agent("coo", "specialist", build_tools)


__all__ = ["register_coo", "build_tools"]
