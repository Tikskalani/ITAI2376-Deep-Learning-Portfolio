"""Atlas -- CFO agent.

Owns the money: imports bank and card statements, models cash flow across
the W-2, Side Shop, and tutoring streams, watches recurring bills,
flags spending anomalies, and produces the monthly P&L. Dispatched by the
CEO. A Sonnet specialist.

register_cfo() attaches it to the dispatcher, wiring in its tools. Call it
once at startup, before any dispatch("cfo", ...).
"""

from __future__ import annotations

from agents.cfo.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_cfo() -> None:
    """Register the CFO agent with the dispatcher, with its tools."""
    register_agent("cfo", "specialist", build_tools)


__all__ = ["register_cfo", "build_tools"]
