"""Tests for the current-API wiring: model-aware request shaping (no
sampling on the 5.5 models, effort by tier, structured outputs), refusal
fallbacks, hosted web tools with pause_turn resumption and per-search
cost, MCP servers from config, and complete_json. No network: the
Anthropic client is a scripted fake."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import costs, llm
from core.config import settings
from core.runtime import agent as rt
from core.runtime.agent import AgentConfig, run_agent
from tests.test_runtime import _agent_env, _fake_client, _response, _text

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _cfg(tier: str = "ceo", **kw) -> AgentConfig:
    return AgentConfig("ceo" if tier == "ceo" else "scout", tier, "system", tools=[], max_turns=4, **kw)


async def test_request_kwargs_shapes_by_model():
    haiku = llm.request_kwargs(model="claude-haiku-4-5-20251001", max_tokens=10, system="s",
                               messages=[], temperature=0.0, effort="high")
    assert haiku["temperature"] == 0.0 and "output_config" not in haiku
    sonnet = llm.request_kwargs(model="claude-sonnet-5-5", max_tokens=10, system="s",
                                messages=[], temperature=0.0, effort="medium")
    assert "temperature" not in sonnet and sonnet["output_config"] == {"effort": "medium"}
    opus = llm.request_kwargs(model="claude-opus-5-5", max_tokens=10, system="s", messages=[],
                              schema={"type": "object"})
    assert opus["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert llm.takes_sampling("claude-haiku-4-5") and not llm.takes_sampling("claude-opus-5-5")


async def test_usage_dict_and_cost_count_web_searches():
    usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=3))
    assert llm.usage_dict(usage) == {"input_tokens": 100, "output_tokens": 10, "web_search_requests": 3}
    assert llm.usage_dict({"input_tokens": 1, "server_tool_use": {"web_search_requests": 2}}) == {
        "input_tokens": 1, "web_search_requests": 2}
    assert costs.cost_of_usage("claude-sonnet-5-5", {"web_search_requests": 5}) == Decimal("0.0500")


async def test_build_request_effort_fallbacks_and_server_tools(monkeypatch):
    monkeypatch.setattr(settings, "refusal_fallbacks", True)
    monkeypatch.setattr(settings, "web_tools", True)
    monkeypatch.setattr(settings, "web_search_max_uses", 3)
    monkeypatch.setattr(settings, "mcp_servers_json", "")
    cfg = _cfg("ceo", server_tools=["web_search"])
    kwargs, betas = rt.build_request(cfg, [{"type": "text", "text": "s"}], [{"role": "user", "content": "hi"}], [])
    assert "temperature" not in kwargs
    assert kwargs["output_config"] == {"effort": settings.effort_ceo}
    assert kwargs["fallbacks"] == "default" and rt._FALLBACK_BETA in betas
    assert kwargs["tools"] == [{"type": rt._WEB_SEARCH_TYPE, "name": "web_search", "max_uses": 3}]

    # Web tools off: the server tool definitions disappear; a scan-tier config gets no fallback.
    monkeypatch.setattr(settings, "web_tools", False)
    kwargs, betas = rt.build_request(_cfg("ceo", server_tools=["web_search", "web_fetch"]), [], [], [])
    assert "tools" not in kwargs
    # The scan tier's request depends on which Haiku is configured; pin each
    # one so the test does not depend on the machine's .env.
    monkeypatch.setattr(settings, "model_scan", "claude-haiku-4-5-20251001")
    scan = AgentConfig("triage", "scan", "s")
    kwargs, betas = rt.build_request(scan, [], [], [])
    assert "fallbacks" not in kwargs and betas == [] and "output_config" not in kwargs
    assert kwargs["temperature"] == 1.0  # Haiku 4.5 still takes sampling
    monkeypatch.setattr(settings, "model_scan", "claude-haiku-5-5")
    kwargs, betas = rt.build_request(AgentConfig("triage", "scan", "s"), [], [], [])
    assert "temperature" not in kwargs and "fallbacks" not in kwargs  # Haiku 5.5 rejects sampling


async def test_build_request_adds_mcp_servers_from_config(monkeypatch):
    monkeypatch.setattr(settings, "refusal_fallbacks", False)
    monkeypatch.setattr(settings, "mcp_servers_json",
        '[{"name":"notion","url":"https://mcp.example/mcp","token":"t","agents":["ceo"],"allowed_tools":["search"]},'
        ' {"name":"other","url":"https://x","agents":["cfo"]}, {"bad": 1}]')
    assert [e["name"] for e in rt.mcp_servers_for("ceo")] == ["notion"]
    assert rt.mcp_servers_for("build") == []
    local = [{"name": "local", "description": "d", "input_schema": {}}]
    # A run that did not ask for remote tools carries none (lever 1, r12).
    plain, plain_betas = rt.build_request(_cfg("ceo"), [], [], local)
    assert "mcp_servers" not in plain and plain["tools"] == local and rt._MCP_BETA not in plain_betas
    kwargs, betas = rt.build_request(_cfg("ceo"), [], [], local, external=True)
    assert kwargs["mcp_servers"] == [{"type": "url", "url": "https://mcp.example/mcp", "name": "notion",
                                      "authorization_token": "t"}]
    assert kwargs["tools"][1] == {"type": "mcp_toolset", "mcp_server_name": "notion",
                                  "configs": [{"name": "search", "enabled": True}],
                                  "default_config": {"enabled": False}}
    assert betas == [rt._MCP_BETA]
    monkeypatch.setattr(settings, "mcp_servers_json", "not json")
    assert rt.mcp_servers_for("ceo") == []


def _server_use():
    return SimpleNamespace(type="server_tool_use", id="srv1", name="web_search", input={"query": "atlas"})


def _usage_with_search(n: int):
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=n))


async def test_run_agent_resumes_a_paused_turn_and_books_searches(monkeypatch):
    monkeypatch.setattr(settings, "web_tools", True)
    monkeypatch.setattr(settings, "mcp_servers_json", "")
    client = _fake_client([
        _response([_server_use(), _text("looking")], "pause_turn", usage=_usage_with_search(2)),
        _response([_text("Found it.")], "end_turn", usage=_usage_with_search(1)),
    ])
    cfg = _cfg("ceo", server_tools=["web_search"])
    with _agent_env(client):
        result = await run_agent(cfg, "search for atlas")
    assert result.text == "Found it." and result.stop_reason == "end_turn" and result.turns == 2
    assert result.usage["web_search_requests"] == 3
    assert [tc for tc in result.tool_calls if tc.get("server")][0]["name"] == "web_search"
    # The paused assistant turn went back as is, with no extra user message.
    second = client.messages.create.await_args_list[1].kwargs["messages"]
    assert second[-1]["role"] == "assistant" and second[-1]["content"][0].type == "server_tool_use"


async def test_run_agent_turns_a_refusal_into_a_plain_sentence(monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers_json", "")
    refused = _response([], "refusal")
    refused.stop_details = SimpleNamespace(type="refusal", category="cyber", explanation="x")
    client = _fake_client([refused])
    with _agent_env(client):
        result = await run_agent(_cfg("ceo"), "do the thing")
    assert result.stop_reason == "refusal" and "declined by a safety check (cyber)" in result.text
    assert client.messages.create.await_count == 1


async def test_complete_json_parses_structured_reply_and_handles_refusal(monkeypatch):
    class _Msgs:
        def __init__(self, reply, stop="end_turn"):
            self.reply, self.stop, self.calls = reply, stop, []

        async def create(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)], stop_reason=self.stop,
                                   stop_details=SimpleNamespace(category="bio") if self.stop == "refusal" else None,
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                         cache_creation_input_tokens=0, cache_read_input_tokens=0))

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    monkeypatch.setattr(settings, "model_scan", "claude-haiku-4-5-20251001")  # pinned, not read from .env
    good = _Msgs('{"ok": true}')
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: SimpleNamespace(messages=good))
    with patch("core.llm._record_usage", new=AsyncMock()):
        assert await llm.complete_json(system="s", user="u", schema=schema, purpose="test-json") == {"ok": True}
        assert good.calls[0]["output_config"]["format"]["schema"] == schema
        assert "temperature" in good.calls[0]  # Haiku 4.5 still takes it
        bad = _Msgs("not json")
        monkeypatch.setattr("core.runtime.agent.get_client", lambda: SimpleNamespace(messages=bad))
        assert await llm.complete_json(system="s", user="u", schema=schema, purpose="test-json") is None
        refused = _Msgs("", stop="refusal")
        monkeypatch.setattr("core.runtime.agent.get_client", lambda: SimpleNamespace(messages=refused))
        assert await llm.complete(system="s", user="u", purpose="test-json") == ""


async def test_registered_agents_carry_their_server_tools():
    from core.runtime.dispatcher import get_registration
    from core.runtime.register_all import register_all

    register_all()
    assert get_registration("news_discovery").overrides["server_tools"] == ["web_search", "web_fetch"]
    assert get_registration("opportunity_scout").overrides["server_tools"] == ["web_search", "web_fetch"]
    assert get_registration("career").overrides["server_tools"] == ["web_search", "web_fetch"]
    assert get_registration("ceo").overrides["server_tools"] == ["web_search"]
    assert "server_tools" not in get_registration("cfo").overrides


def test_haiku_5_5_takes_effort_not_temperature():
    from core.llm import request_kwargs

    new = request_kwargs(model="claude-haiku-5-5", max_tokens=10, system="s", messages=[],
                         temperature=0.0, effort="low")
    assert "temperature" not in new and new["output_config"]["effort"] == "low"
    old = request_kwargs(model="claude-haiku-4-5-20251001", max_tokens=10, system="s", messages=[],
                         temperature=0.0, effort="low")
    assert old["temperature"] == 0.0 and "output_config" not in old


async def test_build_request_caches_the_conversation_and_takes_an_effort_override():
    import core.runtime.agent as rt

    kwargs, _ = rt.build_request(_cfg("ceo"), [], [{"role": "user", "content": "hi"}], [])
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"]["effort"] == rt.settings.effort_ceo
    kwargs, _ = rt.build_request(_cfg("ceo"), [], [{"role": "user", "content": "hi"}], [], effort="medium")
    assert kwargs["output_config"]["effort"] == "medium"


async def test_scan_calls_default_to_low_effort(monkeypatch):
    import core.llm as llm

    seen = {}

    class _Resp:
        stop_reason = "end_turn"
        content = []
        usage = None

    class _Msgs:
        async def create(self, **kw):
            seen.update(kw)
            return _Resp()

    class _Client:
        messages = _Msgs()

    import core.runtime.agent as rt
    monkeypatch.setattr(rt, "get_client", lambda: _Client())
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(llm, "_record_usage", _noop)
    monkeypatch.setattr(llm, "resolve_model", lambda tier, provider=None: "claude-haiku-5-5")
    await llm.complete(system="s", user="u", tier="scan")
    assert seen["output_config"]["effort"] == llm.settings.effort_scan
    assert "temperature" not in seen
