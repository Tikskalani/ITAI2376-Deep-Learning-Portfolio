"""Efficiency lever 2 (r12): every agent's prompt stays inside its budget.

config/prompt_budgets.yaml holds the budgets and explains the units. No model
and no API call: the estimate is characters / 3, rounded up.
"""

from __future__ import annotations

import json
import math

import pytest
import yaml

from core.config import REPO_ROOT, settings
from core.runtime import agent as rt
from core.runtime.dispatcher import _REGISTRY
from core.runtime.register_all import register_all

BUDGETS = yaml.safe_load((REPO_ROOT / "config" / "prompt_budgets.yaml").read_text(encoding="utf-8"))


def _estimate(text: str) -> int:
    return math.ceil(len(text) / 3)


def _config(name: str) -> rt.AgentConfig:
    register_all()
    reg = _REGISTRY[name]
    tools = reg.tools_factory() if reg.tools_factory else []
    return rt.load_agent_config(name, reg.model_tier, tools, **reg.overrides)


def test_every_registered_agent_has_a_budget():
    register_all()
    assert set(_REGISTRY) == set(BUDGETS), "add the new agent to config/prompt_budgets.yaml (or drop the old one)"


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_prompt_and_tools_stay_inside_the_budget(name):
    cfg = _config(name)
    system = _estimate(cfg.system_prompt)
    tools = _estimate(json.dumps([t.to_api_dict() for t in cfg.tools] + rt.server_tool_defs(cfg)))
    budget = BUDGETS[name]
    assert system <= budget["system"], f"{name}: system prompt {system} > budget {budget['system']}"
    assert tools <= budget["tools"], f"{name}: tool definitions {tools} > budget {budget['tools']}"


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_no_agent_carries_remote_tools_unless_the_run_asks(name, monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers_json",
                        '[{"name":"zapier","url":"https://mcp.example/mcp","agents":["all"]}]')
    kwargs, betas = rt.build_request(_config(name), [], [], [])
    assert "mcp_servers" not in kwargs and rt._MCP_BETA not in betas
