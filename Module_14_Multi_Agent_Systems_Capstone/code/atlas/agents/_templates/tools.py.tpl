"""Atlas -- {{NAME}} agent tools."""

from __future__ import annotations

from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    record_lesson_tool,
    remember_fact_tool,
)


def build_tools() -> list[ToolSpec]:
    """Tools available to the {{NAME}} agent."""
    return [
        remember_fact_tool("{{NAME}}"),
        recall_facts_tool(),
        recall_lessons_tool(),
        record_lesson_tool(domain="{{NAME}}"),
    ]


__all__ = ["build_tools"]
