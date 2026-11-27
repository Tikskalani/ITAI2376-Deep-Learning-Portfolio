"""Atlas -- Build agent.

Productizes ideas Trilok has decided to ship. Runs the spec-to-prototype
loop: reads context, drafts the spec, drafts the build plan. Does not
execute code; Trilok or Claude Code does. Dispatched by the CEO. A
Sonnet specialist.
"""

from __future__ import annotations

from agents.build.tools import build_tools
from core.runtime.dispatcher import register_agent


def register_build() -> None:
    register_agent("build", "specialist", build_tools)


__all__ = ["register_build", "build_tools"]
