"""Atlas -- CTO agent tools.

Light toolset for the codebase steward: shared web fetcher (for docs,
PR diffs, forum threads), shared memory tools, and procedure loader.
The CTO drafts and reviews; it does not execute code.
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
        "Load any procedure under procedures/ by its path. The Atlas "
        "procedure library is your bookshelf; read before you write."
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
    from core.config import settings

    # Procedures from tutorials Trilok shared and approved (core/capabilities/):
    # reference data the CTO may consult, never extra permissions.
    capabilities: list[ToolSpec] = []
    if settings.capabilities:
        from core.capabilities.retrieval import tools as capability_tools

        capabilities = capability_tools()
    return capabilities + [
        fetch_url_tool(
            "Use it on documentation pages, GitHub PR diffs, forum threads, "
            "or any URL Trilok forwards for technical review."
        ),
        _LOAD_PROCEDURE,
        recall_facts_tool(),
        remember_fact_tool("cto"),
        recall_lessons_tool(),
        record_lesson_tool(domain="cto"),
    ]


__all__ = ["build_tools"]
