"""Atlas -- Property Ops agent tools.

Light toolset for the property-operations agent. Rent-roll
reconciliation and AEP/Centerpoint portal automation are deferred per
the plan; this first cut handles eviction settlement drafts and
foreclosure tracking through narrative drafts plus the shared memory
and procedure layers.
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
        "Load a property-ops procedure (eviction settlement template, "
        "foreclosure tracking workflow, AEP audit checklist) by its path "
        "under procedures/property_ops/."
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
            "Use it on county records pages, state statutes, property "
            "listings, or any URL Trilok forwards as context for a case."
        ),
        _LOAD_PROCEDURE,
        recall_facts_tool(),
        remember_fact_tool("property_ops"),
        recall_lessons_tool(),
        record_lesson_tool(domain="property_ops"),
    ]


__all__ = ["build_tools"]
