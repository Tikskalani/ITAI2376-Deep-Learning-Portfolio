"""Atlas — agent runtime.

The execution core every Atlas agent runs through. Three public pieces:

  * ToolSpec    — one tool an agent may call (name, schema, async handler).
  * AgentConfig — a dataclass describing one agent: which model tier it
    runs on, its system prompt, and the tools it may call.
  * run_agent   — the agent loop. Opens an episode, calls the Claude
    Messages API, runs the tool-use cycle until the model stops asking
    for tools (or max_turns is hit), logs every tool call to the actions
    table, closes the episode, and returns an AgentResult.

Design notes:
  * Built directly on the anthropic SDK's Messages API, no agent
    framework. Atlas owns the loop so memory and audit logging stay
    under our control.
  * The system prompt is sent as a cache-controlled block, so repeated
    invocations of the same agent only pay to read the cached prefix.
  * A tool handler that raises does NOT crash the loop. The exception is
    turned into an is_error tool_result and handed back to the model,
    which can then recover or report the failure.
  * run_agent stays domain-agnostic. Memory retrieval (e.g. "load the
    CEO's top 5 facts") is the caller's job, passed in via memory_context.

Cost: every run_agent call spends real Claude API tokens billed to the
ANTHROPIC_API_KEY account. AgentResult.usage carries the token counts.
Enforcing settings.daily_usd_cap is deliberately NOT done here yet, that
lands with the Phase 4 token dashboard.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from anthropic import (
    APIConnectionError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core import db
from core.config import REPO_ROOT, settings
from core.costs import enforce_daily_cap
from core.llm import describe_api_error, request_kwargs, usage_dict
from core.memory import end_episode, start_episode
from core.profile import profile_block
from core.projects import projects_block
from core.skills import record_uses, skills_block

log = logging.getLogger(__name__)


# Model tiers come from the locked architecture: Opus for the CEO, Sonnet
# for specialists, Haiku for scans and monitors. Resolved at call time
# through settings.model_for_tier, never copied at import.

# Anthropic exceptions worth retrying. Connection blips, rate limits, and
# 5xx server errors are transient; 4xx (bad request, auth) are not.
_RETRYABLE = (APIConnectionError, RateLimitError, InternalServerError)


# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton, mirrors core.db.get_pool)
# ---------------------------------------------------------------------------

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    """Return the shared AsyncAnthropic client, creating it on first use."""
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


# A tool handler takes the model's input dict and returns any
# JSON-serializable result (or raises, which run_agent catches).
ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolSpec:
    """One tool an agent may call.

    name / description / input_schema are sent to the Messages API in the
    shape it expects. handler is the async function run_agent invokes when
    the model emits a tool_use block for this tool.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_api_dict(self) -> dict[str, Any]:
        """The {name, description, input_schema} dict the Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Everything run_agent needs to execute one agent.

    model_tier is one of "ceo", "specialist", "scan"; the concrete model
    string is resolved from settings via the `model` property so model
    choices stay in one place.
    """

    name: str
    model_tier: str
    system_prompt: str
    tools: list[ToolSpec] = field(default_factory=list)
    max_turns: int = 12
    max_tokens: int = 4096
    temperature: float = 1.0
    # Anthropic-hosted tools this agent may use: "web_search", "web_fetch".
    # They run on Anthropic's side; the loop resumes a paused turn itself.
    server_tools: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        """Resolve model_tier to a concrete Claude model string."""
        try:
            return settings.model_for_tier(self.model_tier)
        except ValueError:
            raise ValueError(
                f"unknown model_tier {self.model_tier!r}; expected one of ['ceo', 'scan', 'specialist']"
            ) from None


# One voice for every agent (audit 2026-10-22, finding 15): the app is his,
# so what he reads talks to him. Stored memory stays in the third person,
# because other agents read it back.
VOICE_RULE = (
    "Voice: anything you write for Trilok to read (replies, briefs, summaries, "
    "notifications, task titles, playbook notes) addresses him as \"you\" and \"your\", "
    "never \"Trilok\", \"he\" or \"his\". Facts saved to memory stay in the third person."
)


def load_agent_config(
    name: str,
    model_tier: str,
    tools: list[ToolSpec] | None = None,
    **overrides: Any,
) -> AgentConfig:
    """Build an AgentConfig, reading the system prompt from agents/<name>/system.md.

    overrides is passed straight through to AgentConfig (max_turns,
    max_tokens, temperature).
    """
    prompt_path = REPO_ROOT / "agents" / name / "system.md"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"no system prompt for agent {name!r} at {prompt_path}"
        )
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not system_prompt:
        raise ValueError(f"system prompt for agent {name!r} is empty")
    return AgentConfig(
        name=name,
        model_tier=model_tier,
        system_prompt=system_prompt + "\n\n" + VOICE_RULE,
        tools=tools or [],
        **overrides,
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Outcome of one run_agent call.

    text         — the model's final assistant text.
    episode_id   — the episode this run was recorded under.
    turns        — how many Messages API round-trips were made.
    stop_reason  — the last stop_reason ("end_turn", "max_tokens", or
                   "max_turns" if our own loop cap was hit).
    tool_calls   — every tool invocation: {name, input, output, is_error}.
    usage        — accumulated token counts across all turns.
    """

    text: str
    episode_id: UUID
    turns: int
    stop_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def run_agent(
    config: AgentConfig,
    user_message: str,
    *,
    participants: list[str] | None = None,
    memory_context: str | None = None,
    goal_id: UUID | None = None,
    external: bool = False,
) -> AgentResult:
    """Run one agent to completion and return its result.

    Opens an episode, drives the Messages API tool-use loop, logs each
    tool call to the actions table, and closes the episode. Raises on an
    unrecoverable API error (after retries); a failing *tool* does not
    raise, it is reported back to the model as an error tool_result.

    memory_context, if given, is prepended to the first user message so
    the model sees retrieved facts/episodes before the actual task.
    external attaches the remote MCP servers (core/runtime/external.py).
    """
    # Enforce the daily cost cap before we burn another token. The
    # check is on-or-over, so setting ATLAS_DAILY_USD_CAP to 0 freezes
    # all model calls without further code changes.
    await enforce_daily_cap()

    # Per-agent monthly cap (no-op when none is set for this agent).
    from core.costs import enforce_agent_cap as _enforce_agent_cap
    await _enforce_agent_cap(config.name)

    episode_id = await start_episode(
        agent_name=config.name,
        participants=participants or ["owner", config.name],
    )

    config = await _policed_server_tools(config, episode_id)
    tools_by_name = {t.name: t for t in config.tools}
    api_tools = [t.to_api_dict() for t in config.tools]

    # System prompt as a cache-controlled block so repeat runs of this
    # agent only pay the cheaper cache-read rate on the prefix.
    system_blocks = [
        {
            "type": "text",
            "text": config.system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # The learned playbook: skills adopted from reels and videos that apply
    # to this agent. A second cached block, so it changes only on adoption.
    skill_ids = await _append_playbook(config.name, system_blocks)

    # Inject today's date so every agent has a wall-clock anchor; without
    # this a dispatched specialist asked for "this month" cannot know what
    # month it is. Goes into the user message, not the system prompt, so
    # the system cache still hits day-to-day.
    today = date.today()
    preamble = f"Today is {today.strftime('%A')}, {today.isoformat()}."
    parts = [preamble]

    # The projects table is the priority list. It goes in the user turn,
    # not the system prompt, so the cached prefix survives a project edit.
    try:
        block = await projects_block("active")
        if block:
            parts.append(f"Projects on record (priority order):\n{block}")
    except Exception:
        log.exception("could not load the projects block")

    # Who's who and what is open, from config/context.yaml (built from his
    # mail history). Static between edits, so it costs little after caching.
    try:
        from core import context_pack
        people = context_pack.people_block()
        if people:
            parts.append(f"People in Trilok's work and life (tier 1 = answer today):\n{people}")
        loops = context_pack.open_loops_block()
        if loops:
            parts.append(loops)
    except Exception:
        log.exception("could not load the context pack")

    # The always-loaded profile: a few lines of standing facts, decisions
    # and dated constraints. Everything else is retrieved on demand.
    try:
        prof = await profile_block()
        if prof:
            parts.append(f"Profile (always in force):\n{prof}")
    except Exception:
        log.exception("could not load the profile block")

    # Per-agent working context: each specialist sees a short reminder of
    # what it last did, so "last time I checked the AEP portal" works.
    try:
        from core.agent_context import recent_context_block
        ctx_block = await recent_context_block(config.name, limit=3)
        if ctx_block:
            parts.append(ctx_block)
    except Exception:
        log.exception("could not load agent working context")

    if memory_context:
        parts.append(memory_context)
    parts.append(user_message)
    first_content = "\n\n---\n\n".join(parts)
    messages: list[dict[str, Any]] = [{"role": "user", "content": first_content}]

    usage_total: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    tool_calls: list[dict[str, Any]] = []
    final_text = ""
    stop_reason = "max_turns"
    turns = 0

    try:
        for turn in range(1, config.max_turns + 1):
            turns = turn
            response = await _create_message(
                config, system_blocks, messages, api_tools, external=external
            )
            _accumulate_usage(usage_total, response.usage)

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                final_text = "\n".join(text_parts).strip()
            stop_reason = response.stop_reason or "end_turn"
            _audit_server_tools(response, tool_calls)

            if stop_reason == "pause_turn":
                # A server tool (web search) paused mid-turn; hand the turn
                # back as is and the API resumes where it stopped.
                messages.append({"role": "assistant", "content": response.content})
                continue
            if stop_reason == "refusal":
                final_text = _refusal_text(response)
                break
            if response.stop_reason != "tool_use":
                break

            # Hand the assistant turn back verbatim, then run the tools it
            # asked for and answer with one tool_result per tool_use block.
            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                spec = tools_by_name.get(block.name)
                output, is_error = await _run_tool(
                    spec, block, config, episode_id, goal_id
                )
                tool_calls.append(
                    {
                        "name": block.name,
                        "input": block.input,
                        "output": output,
                        "is_error": is_error,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _stringify(output),
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # for-else: loop ran to the end without break, cap was hit.
            log.warning(
                "agent %s hit max_turns=%s without finishing",
                config.name,
                config.max_turns,
            )
            stop_reason = "max_turns"
    except Exception as exc:
        # Close the episode so a crashed run still leaves a narrative.
        await end_episode(
            episode_id,
            summary=f"{config.name} failed: {_truncate(user_message, 200)}",
            outcome=f"error: {type(exc).__name__}: {exc}",
            what_changed={"tokens": usage_total, "turns": turns},
            tags=["agent_run", "error", config.name],
            status="failed",
        )
        log.exception("run_agent failed for agent %s", config.name)
        raise

    record = _run_record(config.name, user_message, final_text, stop_reason, turns, len(tool_calls))
    await end_episode(
        episode_id,
        summary=record["summary"],
        outcome=record["outcome"],
        what_changed={
            "tools_used": [tc["name"] for tc in tool_calls],
            "tokens": usage_total,
            "stop_reason": stop_reason,
        },
        tags=["agent_run", config.name],
        status=record["status"],
        result=record["result"],
    )
    await _record_playbook_uses(episode_id, skill_ids)

    # Knowledge-graph activation: fire-and-forget Haiku entity extraction
    # on the CEO's synthesized reply. Only the CEO synthesizes for the
    # user; specialist replies are intermediate and noisy. Errors are
    # swallowed inside link_text; nothing here blocks the user's reply.
    if config.name == "ceo" and final_text and stop_reason == "end_turn":
        try:
            from core.knowledge_graph import link_text_fire_and_forget
            link_text_fire_and_forget(final_text, source=str(episode_id))
        except Exception:
            log.exception("could not schedule knowledge-graph extraction")

    # Per-agent working context: write a short summary so the next
    # dispatch of this agent has continuity. Never blocks the reply.
    if final_text and stop_reason == "end_turn":
        try:
            from core.agent_context import record_context
            await record_context(
                config.name,
                summary=final_text,
                episode_id=episode_id,
            )
        except Exception:
            log.exception("could not record agent working context")

    return AgentResult(
        text=final_text,
        episode_id=episode_id,
        turns=turns,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        usage=usage_total,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def run_agent_stream(
    config: AgentConfig,
    user_message: str,
    *,
    participants: list[str] | None = None,
    memory_context: str | None = None,
    goal_id: UUID | None = None,
    external: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming sibling of run_agent for the interactive chat surface.

    Yields events as the reply is produced, so the UI can show tokens live:
      {"type": "delta",  "text": <token text>}
      {"type": "status", "text": "using <tool>"}   between tool turns
      {"type": "done",   "text": <full reply>, "episode_id", "turns", "stop_reason"}
      {"type": "error",  "text": <message>}
    Additive and self-contained: it does not touch run_agent or dispatch, so
    the non-streaming path stays the safe fallback.
    """
    await enforce_daily_cap()
    from core.costs import enforce_agent_cap as _enforce_agent_cap
    await _enforce_agent_cap(config.name)

    episode_id = await start_episode(
        agent_name=config.name,
        participants=participants or ["owner", config.name],
    )
    config = await _policed_server_tools(config, episode_id)
    tools_by_name = {t.name: t for t in config.tools}
    api_tools = [t.to_api_dict() for t in config.tools]
    system_blocks = [
        {"type": "text", "text": config.system_prompt,
         "cache_control": {"type": "ephemeral"}}
    ]
    skill_ids = await _append_playbook(config.name, system_blocks)
    today = date.today()
    parts = [f"Today is {today.strftime('%A')}, {today.isoformat()}."]
    try:
        from core.agent_context import recent_context_block
        ctx_block = await recent_context_block(config.name, limit=3)
        if ctx_block:
            parts.append(ctx_block)
    except Exception:
        log.exception("could not load agent working context")
    if memory_context:
        parts.append(memory_context)
    parts.append(user_message)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "\n\n---\n\n".join(parts)}]

    usage_total: dict[str, int] = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    tool_calls: list[dict[str, Any]] = []
    final_text = ""
    stop_reason = "max_turns"
    turns = 0
    client = get_client()

    try:
        for turn in range(1, config.max_turns + 1):
            turns = turn
            # Live chat with the CEO runs at ATLAS_EFFORT_CHAT; everything
            # else keeps its tier's effort.
            chat_effort = settings.effort_chat if config.model_tier == "ceo" else None
            kwargs, betas = build_request(config, system_blocks, messages, api_tools, effort=chat_effort,
                                          external=external)
            streamer = client.beta.messages.stream(betas=betas, **kwargs) if betas \
                else client.messages.stream(**kwargs)

            turn_text = ""
            async with streamer as stream:
                async for delta in stream.text_stream:
                    turn_text += delta
                    yield {"type": "delta", "text": delta}
                response = await stream.get_final_message()

            _accumulate_usage(usage_total, response.usage)
            if turn_text.strip():
                final_text = turn_text.strip()
            stop_reason = response.stop_reason or "end_turn"
            _audit_server_tools(response, tool_calls)
            if stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                yield {"type": "status", "text": "still searching"}
                continue
            if stop_reason == "refusal":
                final_text = _refusal_text(response)
                yield {"type": "delta", "text": final_text}
                break
            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield {"type": "status", "text": f"using {block.name}"}
                spec = tools_by_name.get(block.name)
                output, is_error = await _run_tool(
                    spec, block, config, episode_id, goal_id
                )
                tool_calls.append({"name": block.name, "input": block.input,
                                   "output": output, "is_error": is_error})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": _stringify(output), "is_error": is_error})
            messages.append({"role": "user", "content": tool_results})
        else:
            stop_reason = "max_turns"
    except Exception as exc:
        log.exception("run_agent_stream failed for agent %s", config.name)
        try:
            model_name = config.model
        except ValueError:
            model_name = None
        # Tell the surface first: closing the episode embeds its summary, and if
        # that fails too the person would otherwise see nothing at all.
        yield {"type": "error", "text": describe_api_error(exc, model_name)}
        try:
            await end_episode(
                episode_id,
                summary=f"{config.name} failed: {_truncate(user_message, 200)}",
                outcome=f"error: {type(exc).__name__}: {exc}",
                what_changed={"tokens": usage_total, "turns": turns},
                tags=["agent_run", "error", config.name],
                status="failed",
            )
        except Exception:
            log.exception("could not close the failed episode for %s", config.name)
        return

    record = _run_record(config.name, user_message, final_text, stop_reason, turns, len(tool_calls))
    await end_episode(
        episode_id,
        summary=record["summary"],
        outcome=record["outcome"],
        what_changed={"tools_used": [tc["name"] for tc in tool_calls], "tokens": usage_total,
                      "stop_reason": stop_reason},
        tags=["agent_run", config.name],
        status=record["status"],
        result=record["result"],
    )
    await _record_playbook_uses(episode_id, skill_ids)
    if config.name == "ceo" and final_text and stop_reason == "end_turn":
        try:
            from core.knowledge_graph import link_text_fire_and_forget
            link_text_fire_and_forget(final_text, source=str(episode_id))
        except Exception:
            log.exception("could not schedule knowledge-graph extraction")
    if final_text and stop_reason == "end_turn":
        try:
            from core.agent_context import record_context
            await record_context(config.name, summary=final_text, episode_id=episode_id)
        except Exception:
            log.exception("could not record agent working context")

    yield {"type": "done", "text": final_text, "episode_id": str(episode_id),
           "turns": turns, "stop_reason": stop_reason}


async def _append_playbook(agent: str, system_blocks: list[dict[str, Any]]) -> list[UUID]:
    """Add the agent's learned playbook as a second cached system block.
    Returns the skill ids it carries. Never raises."""
    try:
        block, ids = await skills_block(agent)
    except Exception:
        log.exception("could not load the playbook for %s", agent)
        return []
    if block:
        system_blocks.append(
            {"type": "text", "text": block, "cache_control": {"type": "ephemeral"}}
        )
    return ids


async def _record_playbook_uses(episode_id: UUID, skill_ids: list[UUID]) -> None:
    if not skill_ids:
        return
    try:
        await record_uses(episode_id, skill_ids)
    except Exception:
        log.exception("could not record playbook uses for %s", episode_id)


# ---------------------------------------------------------------------------
# Request shaping: effort, fallbacks, server tools, MCP servers
# ---------------------------------------------------------------------------

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MCP_BETA = "mcp-client-2025-11-20"
_WEB_SEARCH_TYPE = "web_search_20260209"
_WEB_FETCH_TYPE = "web_fetch_20260209"


def _effort_for(tier: str) -> str | None:
    return {"ceo": settings.effort_ceo, "specialist": settings.effort_specialist}.get(tier)


async def _policed_server_tools(config: AgentConfig, episode_id: UUID | None = None) -> AgentConfig:
    """The config with only the server tools the policy allows.

    Anthropic runs web_search and web_fetch, so no per-call gate can see
    them; the policy decides whether the agent gets them at all, under the
    pattern "server:<name>". Only auto attaches one: approve cannot be
    honoured for a call Atlas never sees, and unmatched or unavailable
    refuse, as for any tool."""
    if not config.server_tools:
        return config
    from dataclasses import replace

    from core.runtime.policy import announce_refusal, gate_for

    kept: list[str] = []
    for name in config.server_tools:
        gate, pattern = await gate_for(f"server:{name}")
        if gate == "auto":
            kept.append(name)
            continue
        log.warning("agent %s: server tool %s not attached (policy %s, %s)", config.name, name, gate, pattern)
        await announce_refusal(f"server:{name}", gate, pattern, agent=config.name, episode_id=episode_id)
    return config if kept == list(config.server_tools) else replace(config, server_tools=kept)


def server_tool_defs(config: AgentConfig) -> list[dict[str, Any]]:
    """The Anthropic-hosted tool definitions this agent opted into."""
    if not settings.web_tools or not config.server_tools:
        return []
    out: list[dict[str, Any]] = []
    if "web_search" in config.server_tools:
        out.append({"type": _WEB_SEARCH_TYPE, "name": "web_search",
                    "max_uses": max(1, int(settings.web_search_max_uses))})
    if "web_fetch" in config.server_tools:
        out.append({"type": _WEB_FETCH_TYPE, "name": "web_fetch",
                    "max_uses": max(1, int(settings.web_search_max_uses))})
    return out


def mcp_servers_for(agent: str) -> list[dict[str, Any]]:
    """Remote MCP servers configured for this agent (ATLAS_MCP_SERVERS)."""
    raw = (settings.mcp_servers_json or "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("ATLAS_MCP_SERVERS is not valid JSON; ignoring")
        return []
    out: list[dict[str, Any]] = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("name") or not e.get("url"):
            continue
        agents = e.get("agents") or ["ceo"]
        if agent not in agents and "all" not in agents:
            continue
        out.append(e)
    return out


def build_request(
    config: AgentConfig,
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    api_tools: list[dict[str, Any]],
    effort: str | None = None,
    *,
    external: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """The keyword arguments and beta flags for one Messages API call.

    Sampling only where the model accepts it, effort by tier, refusal
    fallbacks on the Opus and Sonnet tiers, the agent's server tools,
    and, when external is set, the remote MCP servers configured for it."""
    kwargs = request_kwargs(
        model=config.model, max_tokens=config.max_tokens, system=system_blocks,
        messages=messages, temperature=config.temperature,
        effort=effort or _effort_for(config.model_tier),
    )
    # Automatic caching: the API caches up to the last block, so each turn of
    # a tool loop reads the earlier turns at the cache rate (a tenth of input)
    # instead of paying for them again. The system block keeps its own mark.
    kwargs["cache_control"] = {"type": "ephemeral"}
    betas: list[str] = []
    tools: list[dict[str, Any]] = list(api_tools)
    tools.extend(server_tool_defs(config))
    # Remote MCP tools only when the run asked for them: the Zapier toolset
    # alone is about 19.6k tokens (core/runtime/external.py).
    servers = mcp_servers_for(config.name) if external else []
    if servers:
        kwargs["mcp_servers"] = [
            {k: v for k, v in {
                "type": "url", "url": e["url"], "name": e["name"],
                "authorization_token": e.get("token") or None,
            }.items() if v is not None}
            for e in servers
        ]
        for e in servers:
            toolset: dict[str, Any] = {"type": "mcp_toolset", "mcp_server_name": e["name"]}
            if e.get("allowed_tools"):
                toolset["configs"] = [{"name": n, "enabled": True} for n in e["allowed_tools"]]
                toolset["default_config"] = {"enabled": False}
            tools.append(toolset)
        betas.append(_MCP_BETA)
    if tools:
        kwargs["tools"] = tools
    if settings.refusal_fallbacks and config.model_tier in ("ceo", "specialist"):
        kwargs["fallbacks"] = "default"
        betas.append(_FALLBACK_BETA)
    return kwargs, betas


def _refusal_text(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    category = getattr(details, "category", None) or "policy"
    return (f"I could not complete that: the request was declined by a safety check "
            f"({category}). Rephrase it or take it up directly.")


async def _create_message(
    config: AgentConfig,
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    api_tools: list[dict[str, Any]],
    *,
    external: bool = False,
) -> Any:
    """One Messages API call, with retry on transient errors."""
    client = get_client()
    kwargs, betas = build_request(config, system_blocks, messages, api_tools, external=external)

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    ):
        with attempt:
            if betas:
                return await client.beta.messages.create(betas=betas, **kwargs)
            return await client.messages.create(**kwargs)
    # AsyncRetrying with reraise=True either returns above or raises; this
    # line is unreachable but keeps the type checker satisfied.
    raise RuntimeError(  # pragma: no cover
        "retry loop exited without a response"
    )


async def _run_tool(
    spec: ToolSpec | None,
    block: Any,
    config: AgentConfig,
    episode_id: UUID,
    goal_id: UUID | None,
) -> tuple[Any, bool]:
    """One tool call, announced on the event stream before and after, so
    mission control shows the agent's hands moving as they move."""
    from core import events

    tool_input = block.input or {}
    await events.emit("run.tool", agent=config.name, episode_id=episode_id, title=block.name,
                      detail=_stringify(tool_input) if tool_input else "")
    output, is_error = await _run_tool_inner(spec, block, config, episode_id, goal_id)
    await events.emit("run.tool.done", agent=config.name, episode_id=episode_id, title=block.name,
                      detail=_stringify(output), data={"ok": not is_error})
    return output, is_error


async def _run_tool_inner(
    spec: ToolSpec | None,
    block: Any,
    config: AgentConfig,
    episode_id: UUID,
    goal_id: UUID | None,
) -> tuple[Any, bool]:
    """Execute one tool_use block. Returns (output, is_error).

    A missing tool or a raising handler is reported as an error rather
    than crashing the loop, so the model gets a chance to recover.
    """
    tool_input = block.input or {}

    if spec is None:
        msg = f"unknown tool {block.name!r}"
        log.warning("agent %s requested %s", config.name, msg)
        await _log_action(
            config.name,
            {"tool": block.name, "input": tool_input},
            {"error": msg},
            episode_id,
            goal_id,
        )
        return msg, True

    # The approval policy runs before the handler. deny, unmatched (no row
    # names the tool) and unavailable (the table could not be read): the
    # model is told, the refusal is audited, and the last two raise an event
    # saying what to fix. approve: an approval card goes to Trilok and the
    # handler runs only on yes. auto: straight through.
    from core.runtime.policy import REFUSING, announce_refusal, gate_for, refusal_message

    gate, pattern = await gate_for(block.name)
    approved_by = "auto"
    if gate in REFUSING:
        msg = refusal_message(block.name, gate, pattern)
        log.warning("agent %s: %s refused by policy (%s, %s)", config.name, block.name, gate, pattern)
        await _log_action(
            config.name,
            {"tool": block.name, "input": tool_input, "policy": pattern, "gate": gate},
            {"error": "denied by policy" if gate == "deny" else f"refused: policy {gate}"},
            episode_id,
            goal_id,
            approved_by="policy",
        )
        await announce_refusal(block.name, gate, pattern, agent=config.name, episode_id=episode_id)
        return msg, True
    ledger_key: str | None = None
    if gate == "approve":
        from core.runtime import ledger
        from core.runtime.approval import request_approval

        # A gated tool has hands. If this exact call already happened (or is
        # waiting, or was refused) inside the same unit of work, a retry
        # gets the record, never a second send.
        scope = ledger.current_scope() or f"episode:{episode_id}"
        key = ledger_key = ledger.call_key(scope, block.name, _jsonsafe(tool_input))
        prior = await ledger.claim(
            key, scope=scope, tool=block.name, agent_name=config.name,
            tool_input=_jsonsafe(tool_input), episode_id=episode_id,
        )
        if prior is not None:
            await _log_action(
                config.name,
                {"tool": block.name, "input": tool_input, "policy": pattern},
                {"ledger": prior.status},
                episode_id,
                goal_id,
                approved_by="policy",
            )
            if prior.status == "done":
                return {"note": prior.message(block.name), "result": prior.result}, False
            return prior.message(block.name), True

        outcome = await request_approval(
            summary=f"{config.name} wants to run {block.name}",
            payload={"tool": block.name, "input": _jsonsafe(tool_input)},
            agent_name=config.name,
            wait_seconds=settings.approval_wait_minutes * 60,
            episode_id=episode_id,
            goal_id=goal_id,
        )
        if not outcome.approved:
            await ledger.settle(
                key,
                outcome.status if outcome.status in ("pending", "rejected", "expired") else "expired",
                approval_id=outcome.approval_id,
            )
            msg = (
                f"Tool {block.name} was not approved ({outcome.status}). "
                + (
                    "The approval card is still open; Trilok can approve it later. "
                    if outcome.status == "pending"
                    else ""
                )
                + "Do not retry it; report what you prepared."
            )
            await _log_action(
                config.name,
                {"tool": block.name, "input": tool_input, "policy": pattern},
                {"error": f"approval {outcome.status}"},
                episode_id,
                goal_id,
                approved_by="user" if outcome.status == "rejected" else "auto",
            )
            return msg, True
        approved_by = "user"
        await ledger.settle(key, "running", approval_id=outcome.approval_id)

    try:
        # The run this call belongs to, for tools that bind what they write to
        # it (core/runtime/run_context.py); reset when the handler returns.
        from core.runtime import run_context

        with run_context.bound(run_context.RunIdentity(episode_id, config.name)):
            result = await spec.handler(tool_input)
    except Exception as exc:
        log.exception("tool %s failed in agent %s", block.name, config.name)
        err = f"{type(exc).__name__}: {exc}"
        if ledger_key is not None:
            from core.runtime import ledger

            # Fail safe: a send can time out after it went through, so a
            # crash counts as "may have acted" and a retry is refused. A
            # handler that knows nothing happened raises ledger.NotStarted,
            # and only then may the call be tried again.
            started = not isinstance(exc, ledger.NotStarted)
            await ledger.settle(ledger_key, "running" if started else "failed", result={"error": err})
        await _log_action(
            config.name,
            {"tool": block.name, "input": tool_input},
            {"error": err},
            episode_id,
            goal_id,
            approved_by=approved_by,
        )
        return f"Tool {block.name} failed: {err}", True

    if ledger_key is not None:
        from core.runtime import ledger

        await ledger.settle(ledger_key, "done", result=_jsonsafe(result))
    await _log_action(
        config.name,
        {"tool": block.name, "input": tool_input},
        {"result": _jsonsafe(result)},
        episode_id,
        goal_id,
        approved_by=approved_by,
    )
    return result, False


async def _log_action(
    agent_name: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    episode_id: UUID,
    goal_id: UUID | None,
    approved_by: str = "auto",
) -> None:
    """Write one tool_call row to the actions audit table.

    approved_by is 'auto' for an ungated call, 'user' when Trilok approved
    or rejected it on a card, and 'policy' when the policy table denied it.
    """
    await db.execute(
        """
        insert into actions
          (agent_name, action_type, inputs, outputs, approved_by,
           episode_id, goal_id)
        values ($1, 'tool_call', $2, $3, $6, $4, $5)
        """,
        agent_name,
        inputs,
        outputs,
        episode_id,
        goal_id,
        approved_by,
    )


def _accumulate_usage(total: dict[str, int], usage: Any) -> None:
    """Add one response's token usage (and web search count) into the total."""
    for key, val in usage_dict(usage).items():
        total[key] = total.get(key, 0) + val


def _audit_server_tools(response: Any, tool_calls: list[dict[str, Any]]) -> None:
    """Record Anthropic-hosted tool calls (web search, web fetch, MCP) in the
    run's tool list so the episode shows what the model looked at."""
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", "")
        if btype in ("server_tool_use", "mcp_tool_use"):
            tool_calls.append({
                "name": getattr(block, "name", btype),
                "input": getattr(block, "input", None),
                "output": "(ran on Anthropic's side)",
                "is_error": False,
                "server": True,
            })


def _stringify(value: Any) -> str:
    """Render a tool result as the string the Messages API expects."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _jsonsafe(value: Any) -> Any:
    """Coerce a tool result into something safe to store in a jsonb column."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return str(value)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _run_record(name: str, ask: str, answer: str, stop_reason: str, turns: int, tool_calls: int) -> dict[str, Any]:
    """summary, outcome, status and result for a finished run.

    The summary carries the ask and the answer, so search, feedback and the
    learning loop see what came back, not only what was asked."""
    answer = " ".join((answer or "").split())
    ask = " ".join((ask or "").split())
    if stop_reason == "end_turn" and answer:
        status = "ok"
        verdict = "answered"
    elif answer:
        status = "incomplete"
        verdict = f"stopped early ({stop_reason}); partial answer"
    else:
        status = "incomplete"
        verdict = f"no answer ({stop_reason})"
    summary = f"{name}: {_truncate(ask, 200)} -> {_truncate(answer, 400) if answer else verdict}"
    outcome = f"{verdict}; {turns} turn{'s' if turns != 1 else ''}, {tool_calls} tool call{'s' if tool_calls != 1 else ''}"
    return {"summary": summary, "outcome": outcome, "status": status, "result": _truncate(answer, 2000) or None}


__all__ = [
    "ToolSpec",
    "ToolHandler",
    "AgentConfig",
    "AgentResult",
    "run_agent",
    "load_agent_config",
    "get_client",
]
