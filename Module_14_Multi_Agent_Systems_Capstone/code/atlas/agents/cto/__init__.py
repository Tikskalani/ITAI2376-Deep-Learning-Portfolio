"""Atlas -- CTO agent.

The executive-layer technologist. Owns the Atlas codebase indirectly,
keeps the procedure library coherent, reviews other agents' tool
outputs. Drafts and opines; never executes code. Dispatched by the
CEO. A Sonnet specialist.
"""

from __future__ import annotations

from agents.cto.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_cto() -> None:
    register_agent("cto", "specialist", build_tools)


__all__ = ["register_cto", "build_tools"]
