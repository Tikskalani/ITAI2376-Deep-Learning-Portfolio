"""Atlas -- Property Ops agent.

Supports the Houston multifamily portfolio Trilok coordinates at Example
Property Group. Drafts eviction settlements, tracks foreclosure
cases, and handles rent-roll / utility questions through narrative
work plus memory and the shared procedure layer. Never sends anything;
every legal or external message is approval-gated. Dispatched by the
CEO. A Sonnet specialist.
"""

from __future__ import annotations

from agents.property_ops.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_property_ops() -> None:
    register_agent("property_ops", "specialist", build_tools)


__all__ = ["register_property_ops", "build_tools"]
