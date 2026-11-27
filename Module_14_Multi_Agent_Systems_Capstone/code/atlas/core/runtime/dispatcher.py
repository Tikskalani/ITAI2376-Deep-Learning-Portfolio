"""Atlas — agent dispatcher.

The CEO (and, later, any agent) hands work to another agent through
dispatch(). The dispatcher owns the agent registry: which model tier each
agent runs on and how to build its tool set. It resolves the name, builds
an AgentConfig, runs the agent via run_agent, and records a 'dispatch' row
in the audit log linked to the episode the agent produced.

Phase 0 note: only the CEO and the Sourcing agent do real work this phase.
The rest of the roster is pre-registered with model tiers so dispatch
fails with a clear, actionable error (a missing agents/<name>/system.md)
rather than a bare KeyError. Their system prompts and tools land later.

Model tiers follow the locked architecture: the CEO runs on Opus, every
specialist on Sonnet. Haiku is reserved for executive-loop scans, which
do not go through dispatch.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from core import db
from core.runtime import ledger
from core.runtime.agent import (
    AgentResult,
    ToolSpec,
    load_agent_config,
    run_agent,
    run_agent_stream,
)
from core.runtime.external import needs_external, wants_external

log = logging.getLogger(__name__)


# A tools factory builds a fresh tool list for one agent run. It is a
# factory rather than a static list so tools that close over per-run state
# (a goal id, an episode) can be rebuilt each dispatch.
ToolsFactory = Callable[[], list[ToolSpec]]


@dataclass
class AgentRegistration:
    """One entry in the agent registry."""

    name: str
    model_tier: str
    tools_factory: ToolsFactory | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, AgentRegistration] = {}

# In-memory dispatch cache keyed by sha256(agent + task) or by an
# explicit idempotency key. A short TTL catches retry storms and
# accidental double-fires without preventing legitimate re-dispatches
# (e.g., a user asking the same question twice on purpose ten minutes
# apart). Process-local; resets on bot restart, which is fine for a
# single-user system.
_DISPATCH_CACHE: dict[str, tuple[float, AgentResult]] = {}
_DISPATCH_CACHE_TTL_S = 60.0


def _dispatch_key(agent: str, task: str) -> str:
    """Default idempotency key when none is supplied."""
    digest = hashlib.sha256(f"{agent}::{task}".encode()).hexdigest()
    return digest[:16]


def clear_dispatch_cache() -> None:
    """Drop every cached dispatch result. Mainly for tests."""
    _DISPATCH_CACHE.clear()


def register_agent(
    name: str,
    model_tier: str,
    tools_factory: ToolsFactory | None = None,
    **overrides: Any,
) -> None:
    """Register (or replace) an agent. overrides pass through to AgentConfig."""
    _REGISTRY[name] = AgentRegistration(
        name=name,
        model_tier=model_tier,
        tools_factory=tools_factory,
        overrides=overrides,
    )


def registered_agents() -> list[str]:
    """Sorted list of registered agent names."""
    return sorted(_REGISTRY)


def get_registration(name: str) -> AgentRegistration | None:
    """Return one agent's registration, or None."""
    return _REGISTRY.get(name)


# --- Roster -----------------------------------------------------------------
# Pre-register the full roster so names resolve before each agent's prompt
# and tools exist. Specialists run on Sonnet; the CEO runs on Opus.

for _specialist in (
    "cfo",
    "coo",
    "cto",
    "property_ops",
    "academic",
    "personal_ops",
    "build",
    "opportunity_scout",
    "news_discovery",
    "client_delivery",
    "career",
):
    register_agent(_specialist, "specialist")
del _specialist

register_agent("ceo", "ceo")


async def dispatch(
    agent_name: str,
    task: str,
    *,
    goal_id: UUID | None = None,
    memory_context: str | None = None,
    dispatched_by: str = "ceo",
    participants: list[str] | None = None,
    idempotency_key: str | None = None,
    external: bool | None = None,
) -> AgentResult:
    """Run a registered agent on `task` and return its AgentResult.

    Raises ValueError for an unregistered name and FileNotFoundError if the
    agent is registered but has no agents/<name>/system.md yet. The dispatch
    itself is logged to the actions table, linked to the agent's episode so
    /why on a dispatch points at the work it produced.
    """
    reg = _REGISTRY.get(agent_name)
    if reg is None:
        raise ValueError(
            f"unknown agent {agent_name!r}; registered: {registered_agents()}"
        )

    # Idempotency: if the same dispatch ran within the last 60 seconds,
    # return the cached result instead of paying for tokens again.
    key = idempotency_key or _dispatch_key(agent_name, task)
    now = time.time()
    cached = _DISPATCH_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _DISPATCH_CACHE_TTL_S:
        log.info(
            "dispatch (cached %.0fs old): %s -> %s",
            now - cached[0],
            dispatched_by,
            agent_name,
        )
        return cached[1]

    tools = reg.tools_factory() if reg.tools_factory is not None else []
    config = load_agent_config(
        name=reg.name,
        model_tier=reg.model_tier,
        tools=tools,
        **reg.overrides,
    )

    log.info("dispatch: %s -> %s", dispatched_by, agent_name)
    from core import events

    # from/target are structured so the Today room can draw the hand-off
    # (core/core_view.py); the title stays for the older feeds.
    await events.emit("dispatch", agent=dispatched_by, title=f"asked {agent_name}", detail=task,
                      data={"from": dispatched_by, "target": agent_name})
    result = None
    attempts = 0
    # One automatic retry. If the first attempt fails OR returns a
    # short/empty reply with a non-end_turn stop reason, the dispatcher
    # tries once more with a nudge prepended to memory_context. Past
    # that, the failure is real and gets surfaced.
    # Both attempts share one ledger scope (unless a workflow step already
    # set one), so the retry never repeats a gated action the first took.
    with ledger.ensure_scope(f"dispatch:{agent_name}:{key}:{uuid4().hex[:12]}"):
        while attempts < 2:
            attempts += 1
            try:
                attempt_context = memory_context
                if attempts == 2:
                    nudge = (
                        "The previous attempt at this task did not complete "
                        "cleanly. Reread your tools, pick the simplest path, "
                        "and finish in fewer steps."
                    )
                    attempt_context = (
                        f"{nudge}\n\n{memory_context}" if memory_context else nudge
                    )
                result = await run_agent(
                    config,
                    task,
                    participants=participants,
                    memory_context=attempt_context,
                    goal_id=goal_id,
                    external=wants_external(dispatched_by, task) if external is None else external,
                )
            except Exception as exc:

                log.warning(
                    "dispatch %s -> %s attempt %d failed: %s",
                    dispatched_by,
                    agent_name,
                    attempts,
                    exc,
                )
                if attempts >= 2:
                    raise
                continue

            # Retry only on an EXPLICIT weak signal. If either attribute is
            # absent (test fixtures often omit them) treat the result as
            # success and break the loop. This keeps the retry from firing
            # spuriously on mocks while still catching real weak outputs.
            stop_reason_v = getattr(result, "stop_reason", None)
            text_v = getattr(result, "text", None)
            weak_stop = stop_reason_v is not None and stop_reason_v != "end_turn"
            weak_text = (
                text_v is not None and len((text_v or "").strip()) == 0
            )
            if not (weak_stop or weak_text):
                break
            log.info(
                "dispatch %s -> %s attempt %d weak (stop=%r, len=%d); retrying",
                dispatched_by,
                agent_name,
                attempts,
                stop_reason_v,
                len(text_v or ""),
            )

    assert result is not None  # one of the branches above sets it

    await db.execute(
        """
        insert into actions
          (agent_name, action_type, inputs, outputs, approved_by,
           episode_id, goal_id)
        values ($1, 'dispatch', $2, $3, 'auto', $4, $5)
        """,
        dispatched_by,
        {"target_agent": agent_name, "task": task},
        {
            "episode_id": str(result.episode_id),
            "turns": result.turns,
            "stop_reason": result.stop_reason,
            "tokens": result.usage,
        },
        result.episode_id,
        goal_id,
    )
    await events.emit(
        "dispatch.done", agent=agent_name, episode_id=result.episode_id,
        title=f"finished for {dispatched_by}",
        data={"from": dispatched_by, "target": agent_name, "ok": result.stop_reason == "end_turn"},
    )
    log.info(
        "dispatch done: %s, turns=%s, stop_reason=%s",
        agent_name,
        result.turns,
        result.stop_reason,
    )
    _DISPATCH_CACHE[key] = (time.time(), result)
    return result


__all__ = [
    "AgentRegistration",
    "ToolsFactory",
    "register_agent",
    "registered_agents",
    "get_registration",
    "dispatch",
    "clear_dispatch_cache",
]


async def dispatch_stream(
    agent_name: str,
    task: str,
    *,
    participants=None,
    memory_context=None,
    goal_id=None,
    thread_id=None,
):
    """Streaming variant of dispatch for the interactive chat surface. Yields
    run_agent_stream events. No idempotency cache, no dispatch-row logging,
    no auto-retry -- the user is watching tokens live and can just resend."""
    reg = _REGISTRY.get(agent_name)
    if reg is None:
        raise ValueError(
            f"unknown agent {agent_name!r}; registered: {registered_agents()}"
        )
    tools = reg.tools_factory() if reg.tools_factory is not None else []
    config = load_agent_config(
        name=reg.name, model_tier=reg.model_tier, tools=tools, **reg.overrides
    )
    log.info("dispatch_stream: %s", agent_name)
    from core import events

    # thread: the Chat conversation it belongs to, so the Today feed can link to it.
    await events.emit("dispatch", agent="owner", title=f"asked {agent_name}", detail=task,
                      data={"from": "owner", "target": agent_name, "thread": thread_id})
    async for event in run_agent_stream(
        config, task, participants=participants,
        memory_context=memory_context, goal_id=goal_id,
        external=needs_external(task),
    ):
        yield event
