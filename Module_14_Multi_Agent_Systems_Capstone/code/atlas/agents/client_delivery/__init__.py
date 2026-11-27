"""Atlas -- Client Delivery agent.

Manages the operational side of any external work Trilok accepts:
scope, milestones, status updates, scope-creep flags, handovers.
Drafts every client-facing message; never sends. Dispatched by the
CEO. A Sonnet specialist.
"""

from __future__ import annotations

from agents.client_delivery.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_client_delivery() -> None:
    register_agent("client_delivery", "specialist", build_tools)


__all__ = ["register_client_delivery", "build_tools"]
