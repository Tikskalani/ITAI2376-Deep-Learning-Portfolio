"""Atlas -- Build agent tools.

Light toolset: shared web fetcher (reference designs, competitor pages),
shared memory tools, procedure loader (for build templates). The agent
produces specs and build plans; it does not execute code.
"""

from __future__ import annotations

from typing import Any

from core.memory.procedural import load_procedure_record
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    record_lesson_tool,
    remember_fact_tool,
)
from core.web import fetch_url_tool


async def _load_procedure(args: dict[str, Any]) -> dict[str, Any]:
    path = (args.get("path") or "").strip()
    if not path:
        return {"error": "path is required and cannot be empty"}
    try:
        return await load_procedure_record(path)
    except FileNotFoundError:
        return {"error": f"no procedure found at {path!r}"}


_LOAD_PROCEDURE = ToolSpec(
    name="load_procedure",
    description=(
        "Load a build procedure (a stored spec template, a build "
        "checklist) by its path under procedures/."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Procedure path."}
        },
        "required": ["path"],
    },
    handler=_load_procedure,
)


def build_tools() -> list[ToolSpec]:
    return [
        fetch_url_tool(
            "Use it on reference designs, competitor homepages, documentation "
            "pages, or any URL Trilok forwards that informs the spec."
        ),
        _LOAD_PROCEDURE,
        recall_facts_tool(),
        remember_fact_tool("build"),
        recall_lessons_tool(),
        record_lesson_tool(domain="build"),
    ]


__all__ = ["build_tools"]
