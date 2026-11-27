"""Atlas -- Personal Ops agent.

Household-domain coordinator: bill renewals, insurance comparisons, ACA,
auto and home logistics. Reads the CFO's household-tagged recurring
bills and the COO's household-tagged deadlines; can create new
household deadlines. Dispatched by the CEO. A Sonnet specialist.

register_personal_ops() attaches it to the dispatcher, wiring in its
tools. Call it once at startup, before any dispatch("personal_ops", ...).
"""

from __future__ import annotations

from agents.personal_ops.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_personal_ops() -> None:
    """Register the Personal Ops agent with the dispatcher."""
    register_agent("personal_ops", "specialist", build_tools)


__all__ = ["register_personal_ops", "build_tools"]
