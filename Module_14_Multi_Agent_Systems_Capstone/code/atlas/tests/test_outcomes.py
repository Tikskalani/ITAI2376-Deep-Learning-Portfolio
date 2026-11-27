"""Outcome learning (core/outcomes.py): every result that comes back lands on
the run that caused it, and a bad one proposes an eval case."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from agents.ceo.tools import build_tools
from core import db, night, outcomes, skills
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
TABLES = ("episodes", "episode_feedback", "eval_candidates", "skills", "skill_uses", "actions", "events",
          "night_tasks", "projects", "job_queue", "job_applications", "job_postings", "job_events",
          "notifications", "push_subscriptions", "system_heartbeats", "approvals", "tool_ledger", "llm_calls")


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        skills.invalidate_cache()
        yield
        skills.invalidate_cache()


async def _run(agent: str = "career", *, task: str | None = "Draft the Databricks cover note",
               result: str = "Dear hiring team...") -> UUID:
    ep = await db.fetchval(
        "insert into episodes (agent_name, summary, started_at, ended_at, status, result) "
        "values ($1, $2, now(), now(), 'ok', $3) returning id",
        agent, f"{agent}: {task} -> {result[:40]}", result,
    )
    if task:
        await db.execute("insert into actions (agent_name, action_type, inputs, outputs, episode_id) "
                         "values ('ceo', 'dispatch', $1, '{}', $2)", {"agent": agent, "task": task}, ep)
    return ep


@aio
async def test_a_bad_outcome_lands_on_the_skills_and_proposes_an_eval_case(world):
    sid = await skills.add_skill("test-name the company once", "Name the company and the role once.", applies_to=["career"])
    ep = await _run()
    await skills.record_uses(ep, [sid])

    out = await outcomes.record(ep, "bad", kind="correction", note="It named the wrong hiring manager.", source="owner/chat")
    assert out["verdict"] == "bad" and out["skills"] == [sid] and out["candidate_id"]
    fb = await db.fetchrow("select verdict, kind, note, source from episode_feedback where episode_id = $1", ep)
    assert fb["verdict"] == "bad" and fb["kind"] == "correction" and fb["source"] == "owner/chat"
    sk = await skills.get_skill(sid)
    assert (sk["good"], sk["bad"]) == (0, 1)
    cand = (await outcomes.candidates())[0]
    assert cand["id"] == out["candidate_id"] and cand["agent"] == "career" and cand["status"] == "proposed"
    assert cand["task"] == "Draft the Databricks cover note" and cand["result"].startswith("Dear hiring team")
    assert cand["note"] == "It named the wrong hiring manager." and cand["kind"] == "correction"

    # Rating the same run bad again updates the candidate instead of adding one.
    again = await outcomes.record(ep, "bad", kind="tap", note="still wrong")
    assert again["candidate_id"] == out["candidate_id"] and len(await outcomes.candidates()) == 1
    assert (await outcomes.candidates())[0]["note"] == "still wrong"
    # A good verdict proposes nothing and the skill's counts follow the latest verdict.
    good = await outcomes.record(ep, "good", kind="tap")
    assert good["candidate_id"] is None and (await skills.get_skill(sid))["good"] == 1
    with pytest.raises(ValueError):
        await outcomes.record(ep, "good", kind="vibes")
    with pytest.raises(ValueError):
        await outcomes.record(uuid4(), "good", kind="tap")


@aio
async def test_candidates_are_dismissed_written_and_exported(world):
    ep = await _run("coo", task="Draft the status update", result="Hi all")
    out = await outcomes.record(ep, "bad", kind="night_review", note="Too long.")
    cid = out["candidate_id"]
    assert (await outcomes.set_candidate(cid, "dismissed"))["status"] == "dismissed"
    assert await outcomes.candidates() == [] and await outcomes.set_candidate(uuid4(), "dismissed") is None
    assert (await outcomes.set_candidate(cid, "proposed"))["status"] == "proposed"
    assert (await outcomes.set_candidate(cid, "written", suite="outcomes"))["suite"] == "outcomes"
    with pytest.raises(ValueError):
        await outcomes.set_candidate(cid, "lost")
    rows = await outcomes.candidates(status=None)
    line = outcomes.to_jsonl(rows)
    assert '"task": "Draft the status update"' in line and '"expected": "Too long."' in line and line.endswith("\n")
    assert outcomes.to_jsonl([]) == ""

    s = await outcomes.summary()
    assert s["this_week"] == {"good": 0, "bad": 1, "good_ratio": 0.0} and s["by_kind"] == {"night_review": {"good": 0, "bad": 1}}
    assert s["candidates"] == {"proposed": 0, "written": 1, "dismissed": 0}


@aio
async def test_the_morning_review_lands_on_the_night_run(world, monkeypatch):
    monkeypatch.setattr(night, "night_of", lambda now=None: date(2026, 10, 11))
    ep = await _run("client_delivery", task="Draft the October status update", result="Draft...")
    t = await night.add_task("Draft the October status update", "A draft in his voice")
    await db.execute("update night_tasks set status = 'done', episode_id = $2, result = 'Draft...' where id = $1", t["id"], ep)
    reviewed = await night.review(t["id"], "rework", "Wrong tone")
    assert reviewed["review"] == "rework"
    fb = await db.fetchrow("select verdict, kind, note from episode_feedback where episode_id = $1", ep)
    assert fb["verdict"] == "bad" and fb["kind"] == "night_review" and fb["note"] == "Wrong tone"
    assert (await outcomes.candidates())[0]["task"] == "Draft the October status update"
    # Accepted is a good outcome; a task without a run leaves nothing behind.
    await db.execute("update night_tasks set review = null where id = $1", t["id"])
    await night.review(t["id"], "accepted")
    assert (await db.fetchval("select verdict from episode_feedback where episode_id = $1", ep)) == "good"
    t2 = await night.add_task("No run", "x")
    await db.execute("update night_tasks set status = 'done' where id = $1", t2["id"])
    assert (await night.review(t2["id"], "rework"))["review"] == "rework"
    assert await db.fetchval("select count(*) from episode_feedback") == 1


@aio
async def test_a_companys_reply_lands_on_the_draft_that_got_it(world):
    ep = await _run("career", task="Draft for Databricks", result="Cover note")
    pid, app_id = uuid4(), uuid4()
    await db.execute("insert into job_applications (id, key, company, role, status) values ($1, 'databricks|fde', 'Databricks', 'FDE', 'applied')", app_id)
    await db.execute("insert into job_queue (posting_id, application_id, draft_episode_id, status) values ($1, $2, $3, 'submitted')",
                     pid, app_id, ep)
    app = {"id": app_id, "company": "Databricks", "role": "FDE", "status": "interview"}
    await outcomes.from_application(app, "applied", "Interview: Databricks")
    fb = await db.fetchrow("select verdict, kind, note, source from episode_feedback where episode_id = $1", ep)
    assert fb["verdict"] == "good" and fb["kind"] == "application" and fb["source"] == "job_mail"
    assert fb["note"].startswith("Databricks: applied -> interview")
    # A no is a bad outcome with an eval case; a move with no draft behind it is nothing.
    await outcomes.from_application({**app, "status": "rejected"}, "interview", "Not moving forward")
    assert (await db.fetchval("select verdict from episode_feedback where episode_id = $1", ep)) == "bad"
    assert len(await outcomes.candidates()) == 1
    await outcomes.from_application({"id": uuid4(), "company": "X", "status": "offer"}, "applied")
    await outcomes.from_application({**app, "status": "screening"}, "screening")       # no move
    assert await db.fetchval("select count(*) from episode_feedback") == 1


@aio
async def test_the_ceo_records_his_verdict(world):
    tools = {t.name: t.handler for t in build_tools()}
    ep = await _run("cfo", task="Reconcile September", result="Done")
    out = await tools["rate_run"]({"run_id": str(ep)[:8], "verdict": "bad", "note": "Missed the HOA payment"})
    assert out == {"recorded": True, "verdict": "bad", "skills_affected": 0, "eval_case_proposed": True}
    assert (await db.fetchval("select kind from episode_feedback where episode_id = $1", ep)) == "correction"
    assert "error" in await tools["rate_run"]({"run_id": "nope", "verdict": "bad"})
    assert "error" in await tools["rate_run"]({"run_id": str(ep), "verdict": "meh"})
    assert "error" in await tools["rate_run"]({"run_id": str(uuid4()), "verdict": "good"})


@aio
async def test_routes(world):
    ep = await _run("academic", task="Outline the ethics essay", result="I. II. III.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.post(f"/api/episodes/{ep}/feedback", json={"verdict": "bad", "note": "Not my voice"})
        assert r.status_code == 200 and r.json()["candidate_id"]
        cid = r.json()["candidate_id"]
        r = await c.get("/api/outcomes")
        assert r.status_code == 200 and r.json()["summary"]["this_week"]["bad"] == 1
        assert [x["id"] for x in r.json()["candidates"]] == [cid]
        assert (await c.post(f"/api/outcomes/candidates/{cid}/dismiss")).json()["status"] == "dismissed"
        assert (await c.get("/api/outcomes")).json()["candidates"] == []
        assert (await c.post(f"/api/outcomes/candidates/{cid}/restore")).json()["status"] == "proposed"
        assert (await c.post(f"/api/outcomes/candidates/{uuid4()}/dismiss")).status_code == 404
        assert (await c.post(f"/api/episodes/{uuid4()}/feedback", json={"verdict": "good"})).status_code == 404


@aio
async def test_the_standard_counts_the_loop(world):
    from core import standard

    ep = await _run("coo", task="Plan the week", result="...")
    await outcomes.record(ep, "bad", kind="tap", note="Missed Thursday")
    row = next(r for r in (await standard.scorecard())["rows"] if r["key"] == "learning")
    assert "1 miss waiting" in row["value"] and row["status"] == "fail" and "1 bad outcome" in row["detail"]
    await outcomes.set_candidate((await outcomes.candidates())[0]["id"], "written", suite="outcomes")
    row = next(r for r in (await standard.scorecard())["rows"] if r["key"] == "learning")
    assert "0 misses waiting" in row["value"] and row["status"] == "pass" and "1 written so far" in row["detail"]
    assert SimpleNamespace is not None
