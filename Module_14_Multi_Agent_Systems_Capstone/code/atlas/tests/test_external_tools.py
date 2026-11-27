"""Lever 1 (r12/efficiency): remote MCP tools ride only on runs that need them.

The Zapier toolset is 19,564 tokens, more than half of a brief's first
request (counted 2026-10-22). No model is called here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.runtime import dispatcher
from core.runtime.external import needs_external, wants_external


@pytest.mark.parametrize("text", [
    "find my emails for today and summarize", "What's on my calendar tomorrow?",
    "open an issue in the atlas repo on GitHub", "draft a reply in Gmail to JC", "use zapier to add it to Google Sheets",
])
def test_messages_that_name_an_external_app_get_the_tools(text):
    assert needs_external(text)


@pytest.mark.parametrize("text", [
    "what should I do this week?", "Draft the Kirkdale proposal outline", "how much did Atlas cost today",
    "It is the evening of Wednesday, October 21, 2026. Produce Trilok's evening brief.",
])
def test_everything_else_runs_without_them(text):
    assert not needs_external(text)


def test_only_the_owners_own_requests_qualify():
    assert wants_external("surface/web", "check my gmail")
    assert wants_external("night", "file the GitHub issue we discussed")
    assert not wants_external("loop/brief/evening", "summarize email and calendar")
    assert not wants_external("ceo", "look in gmail")
    assert not wants_external("surface/web", "plan my week")


async def _dispatched_external(by: str, task: str) -> bool:
    reg = dispatcher.AgentRegistration(name="ceo", model_tier="ceo")
    seen: dict[str, bool] = {}

    async def fake_run(config, t, **kw):
        seen["external"] = kw["external"]
        return dispatcher.AgentResult(text="ok", episode_id=None, stop_reason="end_turn", turns=1,  # type: ignore[call-arg]
                                      tool_calls=[], usage={})

    with (
        patch.dict(dispatcher._REGISTRY, {"ceo": reg}),
        patch("core.runtime.dispatcher.load_agent_config", return_value=object()),
        patch("core.runtime.dispatcher.run_agent", new=fake_run),
        patch("core.runtime.dispatcher._log_dispatch", new=AsyncMock(), create=True),
        patch("core.runtime.dispatcher.db.execute", new=AsyncMock()),
        patch("core.runtime.dispatcher.db.fetchrow", new=AsyncMock(return_value=None)),
        patch("core.runtime.dispatcher.db.fetchval", new=AsyncMock(return_value=None)),
    ):
        try:
            await dispatcher.dispatch("ceo", task, dispatched_by=by)
        except Exception:  # noqa: BLE001 - only the flag handed to run_agent matters here
            pass
    return seen["external"]


async def test_a_brief_never_carries_zapier_and_a_chat_about_gmail_does():
    assert await _dispatched_external("loop/brief/evening", "summarize email and calendar") is False
    assert await _dispatched_external("surface/web", "check my gmail") is True
