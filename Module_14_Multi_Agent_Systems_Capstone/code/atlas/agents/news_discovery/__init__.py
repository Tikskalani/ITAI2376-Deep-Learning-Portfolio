"""Atlas -- News & Discovery agent.

Pulls signal in from the outside world: fetches links Trilok forwards,
reads the content, and decides whether anything is worth keeping. Most
items end as "no action," which is the correct verdict; the agent's job
is separating durable signal from noise rather than summarizing
everything. Dispatched by the CEO. A Sonnet specialist.

register_news_discovery() attaches it to the dispatcher, wiring in its
tools. Call it once at startup, before any dispatch("news_discovery",
...).
"""

from __future__ import annotations

from agents.news_discovery.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_news_discovery() -> None:
    """Register the News & Discovery agent with the dispatcher."""
    register_agent("news_discovery", "specialist", build_tools, server_tools=['web_search', 'web_fetch'])


__all__ = ["register_news_discovery", "build_tools"]
