"""Atlas -- shared agent tools.

Memory tools that more than one agent needs: remember a fact, recall
facts, recall lessons, record a lesson. Built as factories so each
agent gets its own ToolSpec, tagged with its own source where that
matters.

Agent-specific tools (goal management, the decision journal, dispatch,
procedure loading) live in each agent's own tools.py, not here.
"""

from __future__ import annotations

from typing import Any

from core.lessons import record_lesson, search_lessons
from core.memory import add_fact, search_facts
from core.runtime.agent import ToolSpec


def _clamp_limit(raw: Any, default: int = 5) -> int:
    """Coerce a tool's limit argument into the range 1 to 20."""
    try:
        return max(1, min(int(raw or default), 20))
    except (TypeError, ValueError):
        return default


def remember_fact_tool(source: str) -> ToolSpec:
    """Build a remember_fact tool that tags stored facts with `source`."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        content = (args.get("content") or "").strip()
        if not content:
            return {"error": "content is required and cannot be empty"}
        fact_id = await add_fact(content=content, source=source)
        return {"fact_id": str(fact_id), "stored": content}

    return ToolSpec(
        name="remember_fact",
        description=(
            "Store a single durable fact in long-term semantic memory. Use "
            "it for things worth recalling later: decisions, preferences, "
            "commitments, people, numbers, dates, findings. Write one clear, "
            "self-contained statement. Do not store small talk or transient "
            "detail."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The fact to remember, one self-contained sentence."
                    ),
                }
            },
            "required": ["content"],
        },
        handler=handler,
    )


def recall_facts_tool() -> ToolSpec:
    """Build the recall_facts tool: vector search over semantic memory."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required and cannot be empty"}
        facts = await search_facts(query, limit=_clamp_limit(args.get("limit")))
        return {
            "query": query,
            "count": len(facts),
            "facts": [
                {
                    "content": f["content"],
                    "source": f["source"],
                    "score": f.get("score"),
                    "matched": f.get("matched", []),
                }
                for f in facts
            ],
        }

    return ToolSpec(
        name="recall_facts",
        description=(
            "Search long-term semantic memory for facts relevant to a "
            "query. Call it before answering anything that may depend on "
            "what Atlas already knows. Do not guess when you can check."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search memory for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum facts to return (1-20, default 5).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


def recall_lessons_tool() -> ToolSpec:
    """Build the recall_lessons tool: vector search over the lessons store."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required and cannot be empty"}
        lessons = await search_lessons(
            query, limit=_clamp_limit(args.get("limit"))
        )
        return {
            "query": query,
            "count": len(lessons),
            "lessons": [
                {
                    "content": lesson["content"],
                    "domain": lesson["domain"],
                    "score": lesson.get("score"),
                    "matched": lesson.get("matched", []),
                }
                for lesson in lessons
            ],
        }

    return ToolSpec(
        name="recall_lessons",
        description=(
            "Search the lessons store for lessons relevant to a query. "
            "Call it before acting in an area where past experience should "
            "inform you."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search lessons for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lessons to return (1-20, default 5).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


def record_lesson_tool(domain: str | None = None) -> ToolSpec:
    """Build a record_lesson tool. If `domain` is given it is used as the
    default when the agent doesn't pass one; the agent can still override.

    A lesson is the durable takeaway from something that went well or
    badly. Recording one belongs to any agent doing real work, so this
    factory lives next to remember_fact_tool rather than only on the CEO.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        content = (args.get("content") or "").strip()
        if not content:
            return {"error": "content is required and cannot be empty"}
        lesson_domain = (args.get("domain") or "").strip() or domain
        lesson_id = await record_lesson(content=content, domain=lesson_domain)
        return {"lesson_id": str(lesson_id), "recorded": content}

    desc = (
        "Store a distilled lesson, the durable takeaway from something "
        "that went well or badly. Write one clear statement that should "
        "shape how Atlas acts next time. Lessons are how the system gets "
        "wiser, not just larger."
    )
    if domain:
        desc += f" Defaults to domain {domain!r} when none is given."
    return ToolSpec(
        name="record_lesson",
        description=desc,
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The lesson, as one self-contained statement.",
                },
                "domain": {
                    "type": "string",
                    "description": "Area the lesson belongs to. Optional.",
                },
            },
            "required": ["content"],
        },
        handler=handler,
    )


__all__ = [
    "remember_fact_tool",
    "recall_facts_tool",
    "recall_lessons_tool",
    "record_lesson_tool",
]
