"""Atlas -- News & Discovery agent tools.

The agent reads what Trilok forwards (a link, a reel note, a YouTube
video), judges whether there is a durable learning under the sales, and
either records it to memory or proposes it to the learning-candidate
queue for Trilok's nightly review. Distillation and judgment live in the
agent; these tools only fetch and persist.

build_tools() returns the ToolSpec list the runtime hands to the model.
"""

from __future__ import annotations

from typing import Any

from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    record_lesson_tool,
    remember_fact_tool,
)
from core.web import fetch_url_tool


def fetch_transcript_tool() -> ToolSpec:
    """Build the fetch_transcript tool. Works for any video or reel link
    (YouTube, Instagram, etc.): it gets YouTube captions when available and
    otherwise downloads the audio and transcribes it locally. The spoken
    words are where an AI tip actually lives."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from core.transcription import get_transcript

        raw = (args.get("url") or "").strip()
        if not raw:
            return {"error": "url is required"}
        text = await get_transcript(raw)
        if not text:
            return {
                "url": raw,
                "transcript": None,
                "note": (
                    "could not get a transcript (private/blocked download or "
                    "no speech); triage from the title and anything Trilok pasted"
                ),
            }
        return {"url": raw, "transcript": text}

    return ToolSpec(
        name="fetch_transcript",
        description=(
            "Fetch the transcript of a video or reel from any link (YouTube, "
            "Instagram, and others). Uses captions when available, otherwise "
            "downloads the audio and transcribes it. The spoken words are "
            "where an AI tip actually lives, so this is what you triage. "
            "Returns null when no transcript can be obtained."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "A video or reel URL (YouTube, Instagram, etc.).",
                }
            },
            "required": ["url"],
        },
        handler=handler,
    )


def propose_learning_tool() -> ToolSpec:
    """Build the propose_learning tool: queue a vetted, sales-stripped learning
    for Trilok's nightly review. Proposing is not adopting."""

    def _score(args: dict[str, Any], key: str):
        v = args.get(key)
        if v is None:
            return None
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return None

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from core.discovery import create_candidate

        learning = (args.get("learning") or "").strip()
        if not learning:
            return {"error": "learning is required: the sales-stripped takeaway, in one statement"}
        candidate_id = await create_candidate(
            learning=learning,
            source_type=(args.get("source_type") or "manual").strip(),
            source_url=(args.get("source_url") or None),
            creator=(args.get("creator") or None),
            title=(args.get("title") or None),
            novelty=_score(args, "novelty"),
            relevance=_score(args, "relevance"),
            credibility=_score(args, "credibility"),
            verdict=(args.get("verdict") or None),
            rationale=(args.get("rationale") or None),
            dedup_key=(args.get("dedup_key") or None),
        )
        return {"candidate_id": str(candidate_id), "status": "proposed", "learning": learning}

    return ToolSpec(
        name="propose_learning",
        description=(
            "Queue a vetted, sales-stripped learning for Trilok's nightly "
            "review. Use it after you've pulled a real technique out of a reel "
            "or video and judged it worth his time. Give honest novelty, "
            "relevance, and credibility scores (0 to 1) and a one-line "
            "rationale. Proposing is not adopting: nothing in Atlas changes "
            "until Trilok approves it and reviews the actual diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "learning": {
                    "type": "string",
                    "description": "The durable takeaway, one self-contained statement, with all sales and calls-to-action stripped.",
                },
                "source_type": {
                    "type": "string",
                    "description": "Where it came from.",
                    "enum": ["youtube", "instagram", "manual"],
                },
                "source_url": {"type": "string", "description": "Link to the source, if there is one."},
                "creator": {"type": "string", "description": "Who made it, if known."},
                "title": {"type": "string", "description": "Title of the video or reel, if known."},
                "novelty": {"type": "number", "description": "0 to 1: how new versus what Atlas already knows."},
                "relevance": {"type": "number", "description": "0 to 1: fit to Atlas and Trilok's priorities."},
                "credibility": {"type": "number", "description": "0 to 1: how much to trust the claim."},
                "verdict": {"type": "string", "description": "Your call.", "enum": ["keep", "skip"]},
                "rationale": {"type": "string", "description": "One line: why this verdict."},
                "dedup_key": {"type": "string", "description": "Optional short key to avoid re-proposing the same idea."},
            },
            "required": ["learning"],
        },
        handler=handler,
    )


def build_tools() -> list[ToolSpec]:
    """Return the News & Discovery agent's toolset."""
    return [
        fetch_url_tool(
            "Use it on any link Trilok forwards before you triage."
        ),
        fetch_transcript_tool(),
        recall_facts_tool(),
        remember_fact_tool("news_discovery"),
        recall_lessons_tool(),
        record_lesson_tool(domain="news_discovery"),
        propose_learning_tool(),
    ]


__all__ = [
    "build_tools",
    "fetch_transcript_tool",
    "propose_learning_tool",
]
