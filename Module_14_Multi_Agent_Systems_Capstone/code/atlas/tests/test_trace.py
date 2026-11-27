"""The trace: why a run did what it did (core/replay.py trace(), the
/api/trace route, the explain_run MCP tool, and the queue's link to the run
that drafted each item).

Live test database with every table the trace reads as a private copy
(tests/livedb.py), so Trilok's real runs are never read or touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, projects, replay, skills
from core.jobs import queue as apply_queue
from core.jobs import store
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")

TABLES = ("episodes", "actions", "tool_ledger", "approvals", "skill_uses", "skills", "episode_feedback",
          "lessons", "facts", "decisions", "projects", "job_queue", "job_postings", "job_applications",
          "job_events", "job_submissions")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private_tables(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        yield


async def _episode(agent: str, start: datetime, *, ended: bool = True, **over) -> UUID:
    cols = {"summary": f"{agent} did a thing", "outcome": "ok; 3 turns, 3 tool calls", "status": "ok",
            "result": "Here is what I found.", "what_changed": {"tokens": {"input_tokens": 1200, "output_tokens": 300,
                                                                            "cache_read_input_tokens": 8000},
                                                                 "turns": 3, "stop_reason": "end_turn",
                                                                 "tools_used": ["read_resume", "get_posting"]},
            "participants": ["owner", agent], "tags": ["test-trace"], "project_id": None}
    cols.update(over)
    return await db.fetchval(
        """
        insert into episodes (started_at, ended_at, agent_name, participants, summary, outcome, what_changed,
                              tags, status, result, project_id)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) returning id
        """,
        start, start + timedelta(seconds=42) if ended else None, agent, cols["participants"], cols["summary"],
        cols["outcome"], cols["what_changed"], cols["tags"], cols["status"], cols["result"], cols["project_id"],
    )


async def _action(episode: UUID, agent: str, kind: str, inputs: dict, outputs: dict, *, at: datetime,
                  approved_by: str = "auto") -> UUID:
    return await db.fetchval(
        "insert into actions (agent_name, action_type, inputs, outputs, approved_by, episode_id, created_at) "
        "values ($1, $2, $3, $4, $5, $6, $7) returning id",
        agent, kind, inputs, outputs, approved_by, episode, at,
    )


async def _world() -> dict[str, UUID]:
    """A CEO run that dispatched a career run; the career run read, erred
    once, got one gated call approved, waited on one approval, used a
    playbook, and left a lesson, a fact and a decision behind."""
    t0 = datetime(2026, 10, 11, 7, 0, tzinfo=UTC)
    proj = await projects.upsert_project("test-trace", "Trace Project")
    parent = await _episode("ceo", t0, summary="Planned the morning")
    child = await _episode("career", t0 + timedelta(seconds=5), project_id=proj.id,
                           summary="Tailored the Databricks application")
    # The dispatch is recorded under the child, by the parent's name, while the parent was open.
    await _action(child, "ceo", "dispatch", {"target_agent": "career", "task": "Tailor the Databricks application."},
                  {"episode_id": str(child), "turns": 3, "stop_reason": "end_turn"}, at=t0 + timedelta(seconds=30))
    await _action(child, "career", "tool_call", {"tool": "read_resume", "input": {}},
                  {"result": {"text": "Trilok Kalani ..."}}, at=t0 + timedelta(seconds=6))
    await _action(child, "career", "tool_call", {"tool": "get_posting", "input": {"id": "p1"}},
                  {"error": "no posting p1"}, at=t0 + timedelta(seconds=7))
    await _action(child, "career", "tool_call", {"tool": "save_materials", "input": {"kind": "resume", "text": "x" * 50}},
                  {"result": {"saved": "resume"}}, at=t0 + timedelta(seconds=8), approved_by="user")
    approval = await db.fetchval(
        "insert into approvals (agent_name, action_summary, action_payload, status) values ($1, $2, $3, 'pending') "
        "returning id", "career", "Send the follow-up to Databricks?", {"to": "x"},
    )
    await db.execute(
        "insert into tool_ledger (key, scope, tool, agent_name, status, input, episode_id, approval_id) "
        "values ($1, $2, 'send_email', 'career', 'pending', $3, $4, $5)",
        f"test-trace:{uuid4().hex[:8]}", f"episode:{child}", {"to": "x"}, child, approval,
    )
    skill = await skills.add_skill("test-Reply in one screen", "Lead with the ask.", applies_to=["career"])
    await db.execute("insert into skill_uses (skill_id, episode_id) values ($1, $2)", skill, child)
    await db.execute("insert into episode_feedback (episode_id, verdict, source) values ($1, 'good', 'test')", child)
    await db.execute("insert into lessons (content, domain, source_episode) values ($1, 'test-trace', $2)",
                     "Name the role in the first line.", child)
    await db.execute("insert into facts (content, episode_id, source) values ($1, $2, 'test-trace')",
                     "Databricks asks for a Country field.", child)
    await db.execute("insert into decisions (summary, episode_id) values ($1, $2)", "Apply to Databricks", child)
    return {"parent": parent, "child": child, "project": proj.id, "approval": approval, "skill": skill}


# ------------------------------------------------------------------ trace()


@aio
async def test_trace_tells_the_whole_story_of_a_run():
    w = await _world()
    t = await replay.trace(str(w["child"]))
    assert t is not None
    assert json.dumps(t)  # JSON-safe end to end

    ep = t["episode"]
    assert ep["id"] == str(w["child"]) and ep["agent"] == "career" and ep["duration_s"] == 42.0
    assert ep["turns"] == 3 and ep["stop_reason"] == "end_turn" and ep["tools_used"] == ["read_resume", "get_posting"]
    assert t["project"]["slug"] == "test-trace"
    assert t["trigger"]["by"] == "ceo" and t["trigger"]["task"] == "Tailor the Databricks application."

    steps = t["steps"]
    assert [s["tool"] for s in steps] == ["read_resume", "get_posting", "save_materials"]
    assert steps[0]["ok"] and steps[0]["result"] == {"text": "Trilok Kalani ..."} and steps[0]["input"] == {}
    assert not steps[1]["ok"] and steps[1]["error"] == "no posting p1" and steps[1]["result"] is None
    assert steps[2]["gated"] and steps[2]["approved_by"] == "user"

    assert [r["tool"] for r in t["ledger"]] == ["send_email"] and t["ledger"][0]["status"] == "pending"
    assert [a["id"] for a in t["approvals"]] == [str(w["approval"])] and t["approvals"][0]["status"] == "pending"
    assert [k["title"] for k in t["skills"]] == ["test-Reply in one screen"]
    assert [f["verdict"] for f in t["feedback"]] == ["good"]
    assert [x["content"] for x in t["produced"]["lessons"]] == ["Name the role in the first line."]
    assert [x["content"] for x in t["produced"]["facts"]] == ["Databricks asks for a Country field."]
    assert [x["summary"] for x in t["produced"]["decisions"]] == ["Apply to Databricks"]
    assert t["produced"]["children"] == []

    assert t["cost"]["tokens"]["input_tokens"] == 1200 and float(t["cost"]["usd"]) > 0
    assert "sonnet" in t["cost"]["model"] or "opus" in t["cost"]["model"] or "haiku" in t["cost"]["model"]
    assert t["flags"] == ["1 tool call errored", "an action went through an approval", "waiting for your approval"]


@aio
async def test_parent_run_lists_what_it_dispatched_and_prefixes_resolve():
    w = await _world()
    t = await replay.trace(str(w["parent"])[:8])  # an 8-character prefix is enough
    assert t is not None and t["episode"]["id"] == str(w["parent"])
    assert [c["id"] for c in t["produced"]["children"]] == [str(w["child"])]
    assert t["trigger"] is None and t["flags"] == []


@aio
async def test_unknown_ids_and_open_runs():
    assert await replay.trace(str(uuid4())) is None
    assert await replay.trace("zz") is None
    open_run = await _episode("coo", datetime.now(UTC), ended=False, status=None)
    t = await replay.trace(str(open_run))
    assert t is not None and t["episode"]["duration_s"] is None and "still running" in t["flags"]


@aio
async def test_detail_cap_keeps_payloads_readable():
    ep = await _episode("cfo", datetime.now(UTC), result="r" * 100)
    await _action(ep, "cfo", "tool_call", {"tool": "import_statement", "input": {"rows": ["x" * 40] * 10}},
                  {"result": "y" * 100}, at=datetime.now(UTC))
    t = await replay.trace(str(ep), detail_chars=60)
    assert t["episode"]["result"].endswith("…") and len(t["episode"]["result"]) == 60
    assert isinstance(t["steps"][0]["input"], str) and t["steps"][0]["input"].endswith("…")
    assert t["steps"][0]["result"].endswith("…")
    full = await replay.trace(str(ep), detail_chars=None)
    assert full["episode"]["result"] == "r" * 100 and full["steps"][0]["result"] == "y" * 100


# ------------------------------------------------------------------ the route and the MCP tool


@aio
async def test_route_serves_the_trace_and_refuses_junk():
    w = await _world()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get(f"/api/trace/{w['child']}")
        assert r.status_code == 200 and r.json()["trigger"]["by"] == "ceo"
        assert (await c.get(f"/api/trace/{str(w['child'])[:12]}")).status_code == 200
        assert (await c.get(f"/api/trace/{uuid4()}")).status_code == 404
        assert (await c.get("/api/trace/zzz")).status_code == 404
        assert (await c.get("/api/trace/select%20*%20from%20x")).status_code == 404


@aio
async def test_mcp_explain_run():
    from core import mcp_server as m

    w = await _world()
    out = await m.explain_run(str(w["child"])[:10])
    assert out["episode"]["agent"] == "career" and len(out["steps"]) == 3
    assert "error" in await m.explain_run("nope")


# ------------------------------------------------------------------ the queue remembers its run


@aio
async def test_queue_items_link_to_the_run_that_drafted_them():
    w = await _world()
    pid, _ = await store.upsert_posting({"source": "greenhouse", "board": "testco", "external_id": "t1",
                                         "company": "Testco Labs", "title": "Applied AI Engineer"})
    app_row = await store.upsert_application("Testco Labs", "Applied AI Engineer", status="interested",
                                             source="queue", posting_id=pid)
    await db.execute(
        "insert into job_queue (posting_id, application_id, status, draft_episode_id) values ($1, $2, 'ready', $3)",
        pid, app_row["id"], w["child"],
    )
    items = [i for i in await apply_queue.listing() if i["posting_id"] == pid]
    assert items and items[0]["draft_episode_id"] == w["child"]
