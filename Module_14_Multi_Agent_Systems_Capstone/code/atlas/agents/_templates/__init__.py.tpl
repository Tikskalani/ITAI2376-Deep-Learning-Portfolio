"""Atlas -- {{NAME}} agent registration."""

from __future__ import annotations

from core.runtime.dispatcher import register_agent

from agents.{{NAME}}.tools import build_tools


def register_{{NAME}}() -> None:
    register_agent(
        "{{NAME}}",
        model_tier="{{TIER}}",
        tools_factory=build_tools,
    )


__all__ = ["register_{{NAME}}"]
