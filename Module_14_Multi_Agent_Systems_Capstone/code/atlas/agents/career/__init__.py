"""Atlas -- Career agent.

Owns the job search: the application tracker, discovered roles, tailored
materials and interview prep. Drafts only; never applies, sends or
submits. A Sonnet specialist with web search for company research.

register_career() attaches it to the dispatcher. Call once at startup.
"""

from __future__ import annotations

from agents.career.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_career() -> None:
    """Register the Career agent with the dispatcher."""
    register_agent("career", "specialist", build_tools, server_tools=["web_search", "web_fetch"])


__all__ = ["build_tools", "register_career"]
