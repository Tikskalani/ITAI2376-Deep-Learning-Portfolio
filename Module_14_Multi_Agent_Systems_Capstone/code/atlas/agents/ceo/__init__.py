"""Atlas -- CEO agent.

Executive function: holds goals, dispatches specialists, runs briefs, and
decides when to interrupt Trilok. Phase 0 Week 1 is the minimal version:
the CEO can converse over Telegram and read/write semantic memory.

register_ceo() attaches the CEO (with its tools) to the dispatcher. Call
it once at startup, before any dispatch("ceo", ...).
"""

from __future__ import annotations

from agents.ceo.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_ceo() -> None:
    """Register the CEO with the dispatcher, wiring in its current tools."""
    register_agent("ceo", "ceo", build_tools, server_tools=['web_search'])


__all__ = ["register_ceo", "build_tools"]
