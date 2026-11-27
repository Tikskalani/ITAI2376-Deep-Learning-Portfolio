"""Atlas -- Academic agent tools.

A deliberately small toolset. The Academic agent is a generator: it reads
what memory knows about Trilok's coursework, optionally loads a class
procedure, and writes in his voice. Persistence and tracking (deadlines,
decisions, money) live with other agents. Submitting work is never the
agent's job; Trilok reviews and posts.

build_tools() returns the ToolSpec list the runtime hands to the model.
The dispatcher calls it through the Academic agent's registration (see
agents/academic register_academic).
"""

from __future__ import annotations

from typing import Any

from core.memory.procedural import load_procedure_record
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    remember_fact_tool,
)


async def _load_procedure(args: dict[str, Any]) -> dict[str, Any]:
    """Load a procedure's full markdown by path."""
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
        "Load a procedure's full text by its path, e.g. "
        "'academic/cs_discussion_post.md'. Use this when a class has a "
        "stored writing template, so the draft fits how that class "
        "expects it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Procedure path under the procedures directory.",
            }
        },
        "required": ["path"],
    },
    handler=_load_procedure,
)


def build_tools() -> list[ToolSpec]:
    """Return the Academic agent's toolset. Called once per dispatch."""
    return [
        _LOAD_PROCEDURE,
        recall_facts_tool(),
        remember_fact_tool("academic"),
        recall_lessons_tool(),
    ]


__all__ = ["build_tools"]
