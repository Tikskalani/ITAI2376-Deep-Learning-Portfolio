"""The CEO's tools for the rest of the house: the night shift and the trace.

The Planner tools are covered in tests/test_planner.py. These run the two
handlers the way the model would, against private copies of the tables.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.ceo.tools import build_tools
from core import db, night
from core.projects import upsert_project
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")


def _tool(name: str):
    return {t.name: t.handler for t in build_tools()}[name]


def test_the_roster_of_tools_names_the_house():
    names = [t.name for t in build_tools()]
    for expected in ("add_task", "plan_week", "complete_task", "queue_for_tonight", "explain_run", "dispatch_agent"):
        assert expected in names
    assert names.index("queue_for_tonight") < names.index("dispatch_agent")


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, "night_tasks", "projects", "episodes", "actions", "decisions", "lessons",
                             "approvals", "tool_ledger", "skill_uses", "facts", "notifications"):
        await upsert_project("sidework", "Sidework", kind="own")
        yield


@aio
async def test_queue_for_tonight_puts_a_task_on_the_shift(world, monkeypatch):
    monkeypatch.setattr(night, "night_of", lambda now=None: date(2026, 10, 11))
    out = await _tool("queue_for_tonight")({"title": "Draft the October status update",
                                             "definition_of_done": "A draft in his voice, under 200 words",
                                             "project": "sidework", "agent": "client_delivery"})
    assert out["night"] == "2026-10-11" and out["agent"] == "client_delivery" and out["status"] == "approved"
    assert "morning report" in out["note"]
    row = await db.fetchrow("select title, proposed_by, definition_of_done, project_id from night_tasks where id = $1", out["task_id"])
    assert row["proposed_by"] == "ceo" and row["project_id"] is not None and row["definition_of_done"].startswith("A draft")
    with pytest.raises(ValueError):
        await _tool("queue_for_tonight")({"title": "No done", "definition_of_done": ""})
    with pytest.raises(ValueError):
        await _tool("queue_for_tonight")({"title": "Bad agent", "definition_of_done": "x", "agent": "nobody"})


@aio
async def test_explain_run_reads_the_trace_or_says_no_such_run(world):
    ep = await db.fetchval(
        "insert into episodes (agent_name, summary, started_at, ended_at, outcome, status) "
        "values ('career', 'career: draft -> done', now(), now(), 'answered', 'ok') returning id"
    )
    await db.execute("insert into actions (agent_name, action_type, inputs, outputs, episode_id) "
                     "values ('career', 'tool_call', $1, $2, $3)", {"tool": "read_resume", "input": {}}, {"result": "ok"}, ep)
    out = await _tool("explain_run")({"run_id": str(ep)[:8]})
    assert out["episode"]["id"] == str(ep) and out["episode"]["agent"] == "career"
    assert [s["tool"] for s in out["steps"]] == ["read_resume"]
    assert "error" in await _tool("explain_run")({"run_id": str(uuid4())})
    assert "error" in await _tool("explain_run")({"run_id": ""})
