"""Atlas -- Client Delivery agent tools.

Light toolset: shared web fetcher, shared memory tools, procedure
loader. The agent drafts; it never sends. Deadlines for client
milestones flow through the COO (the CEO dispatches the COO when this
agent flags a real date worth tracking).
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
        "Load a client-delivery procedure by path: a status-update template, "
        "a scope-change letter, a kickoff checklist, a handover template."
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


async def _draft_email(args: dict[str, Any]) -> dict[str, Any]:
    """Create a Gmail draft for Trilok to review and send himself."""
    from core.connectors.gmail import GmailConnector
    from core.connectors.google_auth import configured

    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not to or not subject or not body:
        return {"error": "to, subject and body are all required"}
    if not configured():
        return {
            "error": "Gmail is not connected (ATLAS_GOOGLE_* blank). Put the draft text in "
            "your report so Trilok can paste it.",
            "configured": False,
        }
    try:
        out = await GmailConnector().create_draft(
            to,
            subject,
            body,
            cc=(args.get("cc") or "").strip() or None,
            thread_id=(args.get("thread_id") or "").strip() or None,
            in_reply_to=(args.get("in_reply_to") or "").strip() or None,
        )
    except Exception as exc:
        return {"error": f"could not create the draft: {exc}"}
    return {"configured": True, "sent": False, **out}


_DRAFT_EMAIL = ToolSpec(
    name="draft_email",
    description=(
        "Create a Gmail draft in Trilok's mailbox for him to review and send. "
        "Atlas never sends mail; this only prepares it. Use thread_id and "
        "in_reply_to (from a signal's raw data) to draft a reply inside the "
        "existing thread. Write in his voice: plain prose, no markdown, no "
        "em-dashes, no 'let me know'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body, in Trilok's voice."},
            "cc": {"type": "string", "description": "Optional cc addresses."},
            "thread_id": {"type": "string", "description": "Gmail thread id to reply inside. Optional."},
            "in_reply_to": {"type": "string", "description": "Message-Id header of the mail being answered. Optional."},
        },
        "required": ["to", "subject", "body"],
    },
    handler=_draft_email,
)


def build_tools() -> list[ToolSpec]:
    return [
        fetch_url_tool(
            "Use it on briefs, shared spec documents, competitor examples, or "
            "any URL a client forwards as reference."
        ),
        _LOAD_PROCEDURE,
        _DRAFT_EMAIL,
        recall_facts_tool(),
        remember_fact_tool("client_delivery"),
        recall_lessons_tool(),
        record_lesson_tool(domain="client_delivery"),
    ]


__all__ = ["build_tools"]
