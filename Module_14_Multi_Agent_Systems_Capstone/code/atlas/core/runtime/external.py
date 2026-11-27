"""Atlas -- when a run carries the remote MCP tools (Zapier and the like).

The Zapier toolset is 19,564 tokens of tool definitions (14 tools, counted
2026-10-22 with the token-count endpoint), more than half of the CEO's first
request. Attached to every run, each brief wrote it to the prompt cache at
1.25x the Opus input rate and never called one of those tools.

So remote tools ride only on runs where the owner's own words need one: a
chat or ingest message, or an approved night task, that names an external
app. Briefs, triggers, scans and specialist dispatches never carry them.
The check is a word match, no model involved; a false positive costs one
attach, a miss is fixed by naming the app ("check my Gmail").
"""

from __future__ import annotations

import re

_APP_WORDS = re.compile(
    r"\b(zapier|gmail|e-?mails?|inbox|outlook|calendars?|meetings?|invites?|"
    r"github|issues?|repos?|pull requests?|google (?:docs?|drive|sheets?|tasks|contacts)|"
    r"drive|sheets?|spreadsheets?|slack|notion|zoom|linkedin|trello|asana|hubspot|"
    r"airtable|dropbox|calendly|todoist|canva)\b",
    re.IGNORECASE,
)

# Runs that start from Trilok himself: the chat and ingest surfaces and the
# night tasks he approved. Everything else is Atlas talking to itself.
_OWNER_SOURCES = ("surface/", "night")


def needs_external(text: str) -> bool:
    """True when the words name an app only a remote MCP server reaches."""
    return bool(_APP_WORDS.search(text or ""))


def wants_external(dispatched_by: str, task: str) -> bool:
    """Whether a dispatched run should carry the remote MCP tools."""
    return (dispatched_by or "").startswith(_OWNER_SOURCES) and needs_external(task)


__all__ = ["needs_external", "wants_external"]
