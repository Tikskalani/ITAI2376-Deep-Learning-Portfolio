"""Self-written skills (core/reflection.py): the agent writes down what worked,
he adopts it from the Learn tab."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio

from core import db, discovery, learning_implement, reflection, skills
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
NOW = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)
TABLES = ("episodes", "actions", "episode_feedback", "reflections", "learning_candidates", "skills",
          "skill_uses", "lessons", "llm_calls", "procedures")


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, *TABLES):
        skills.invalidate_cache()
        yield
        skills.invalidate_cache()


async def _run(agent: str = "coo", *, tools: int = 3, hours_ago: float = 2, status: str = "ok",
               task: str = "Pull the September statement and reconcile it", verdict: str | None = None) -> UUID:
    ended = NOW - timedelta(hours=hours_ago)
    ep = await db.fetchval(
        "insert into episodes (agent_name, summary, started_at, ended_at, status, result) "
        "values ($1, $2, $3, $4, $5, 'Reconciled; two items flagged.') returning id",
        agent, f"{agent}: {task}", ended - timedelta(minutes=3), ended, status,
    )
    await db.execute("insert into actions (agent_name, action_type, inputs, outputs, episode_id, created_at) "
                     "values ('ceo', 'dispatch', $1, '{}', $2, $3)", {"agent": agent, "task": task}, ep, ended - timedelta(minutes=3))
    for n in range(tools):
        await db.execute(
            "insert into actions (agent_name, action_type, inputs, outputs, episode_id, created_at) values ($1, 'tool_call', $2, $3, $4, $5)",
            agent, {"tool": f"tool_{n}", "input": {"n": n}}, {"result": f"ok {n}"}, ep, ended - timedelta(minutes=2) + timedelta(seconds=n),
        )
    if verdict:
        await db.execute("insert into episode_feedback (episode_id, verdict) values ($1, $2)", ep, verdict)
    return ep


def _choose(answer: dict | None):
    calls: list[dict] = []

    async def choose(**kw):
        calls.append(kw)
        return answer
    choose.calls = calls  # type: ignore[attr-defined]
    return choose


KEEP = {"worth_keeping": True, "title": "Reconcile a statement in three passes",
        "playbook": "1. Pull the statement before touching the ledger.\n- Match totals first, then lines.\n* Flag anything over $500 for him.",
        "why": "The order avoided a double count."}


@aio
async def test_candidates_are_finished_runs_with_hands_used_not_rated_bad_and_not_read_before(world):
    good = await _run(verdict="good")
    plain = await _run("career", tools=4)
    await _run(tools=2)                        # too few tool calls
    await _run(status="failed")                # not finished well
    await _run(hours_ago=60)                   # outside the window
    await _run(verdict="bad")                  # a miss is an eval case, not a skill
    read = await _run("cto")
    await db.execute("insert into reflections (episode_id, worth) values ($1, false)", read)
    rows = await reflection.candidates(now=NOW)
    assert [r["id"] for r in rows] == [good, plain]          # his good rating first
    assert rows[1]["tool_calls"] == 4
    assert [r["id"] for r in await reflection.candidates(now=NOW, cap=1)] == [good]


@aio
async def test_a_worthwhile_run_becomes_a_drafted_learning_he_can_adopt(world):
    ep = await _run("cfo", verdict="good")
    chooser = _choose(KEEP)
    out = await reflection.reflect_one({"id": ep, "agent_name": "cfo", "ended_at": NOW}, choose=chooser)
    assert out["worth"] and out["title"] == "Reconcile a statement in three passes"
    record = chooser.calls[0]["user"]
    assert "Agent: cfo" in record and "Task: Pull the September statement" in record and "1. tool_0({'n': 0}) -> ok 0" in record
    assert "Result: Reconciled" in record and chooser.calls[0]["purpose"] == "reflection"

    cand = await discovery.get_candidate(out["candidate_id"])
    assert cand["status"] == "drafted" and cand["source_type"] == "reflection" and cand["creator"] == "cfo"
    assert cand["source_url"] == f"atlas://run/{ep}" and cand["dedup_key"] == "reflection:reconcile-a-statement-in-three-passes"
    draft = cand["metadata"]["draft"]
    assert draft["kind"] == "playbook" and draft["applies_to"] == ["cfo"]
    assert draft["playbook"] == ("Pull the statement before touching the ledger.\nMatch totals first, then lines.\n"
                                 "Flag anything over $500 for him.")
    queue = await discovery.list_review_queue()
    assert [c["id"] for c in queue] == [cand["id"]]
    ref = await db.fetchrow("select candidate_id, worth, note from reflections where episode_id = $1", ep)
    assert ref["candidate_id"] == cand["id"] and ref["worth"] and ref["note"] == "The order avoided a double count."

    # Adopt into Atlas: the existing path, a trial skill for the CFO.
    adopted = await learning_implement.adopt_candidate(cand["id"])
    sk = await skills.get_skill(UUID(adopted["skill_id"]))
    assert sk["status"] == "trial" and sk["applies_to"] == ["cfo"] and sk["title"] == "Reconcile a statement in three passes"
    assert (await discovery.get_candidate(cand["id"]))["status"] == "adopted"

    # The same title is not proposed twice; a run with nothing in it is remembered as read.
    ep2 = await _run("cfo")
    out2 = await reflection.reflect_one({"id": ep2, "agent_name": "cfo", "ended_at": NOW}, choose=chooser)
    assert out2["worth"] is False and out2.get("duplicate") and await db.fetchval("select count(*) from learning_candidates") == 1
    ep3 = await _run("coo")
    out3 = await reflection.reflect_one({"id": ep3, "agent_name": "coo", "ended_at": NOW},
                                        choose=_choose({"worth_keeping": False, "title": "", "playbook": "", "why": "a one-off"}))
    assert out3["worth"] is False and (await db.fetchval("select note from reflections where episode_id = $1", ep3)) == "a one-off"
    assert await reflection.candidates(now=NOW) == []


@aio
async def test_run_reads_each_run_once_and_survives_the_model(world):
    await _run("coo")
    await _run("career", verdict="good")
    assert await reflection.run(now=NOW, choose=_choose(KEEP)) == {"read": 2, "proposed": 1}
    assert await reflection.run(now=NOW, choose=_choose(KEEP)) == {"read": 0, "proposed": 0}

    async def broken(**kw):
        raise RuntimeError("api down")
    ep = await _run("academic")
    assert await reflection.run(now=NOW, choose=broken) == {"read": 1, "proposed": 0}
    assert (await db.fetchval("select note from reflections where episode_id = $1", ep)) == "no answer from the model"
    assert await reflection.run(now=NOW, choose=_choose(None)) == {"read": 0, "proposed": 0}


def test_the_playbook_is_cleaned_and_bounded():
    assert reflection._clean_playbook("1) one\n\n- two\n* three\n") == "one\ntwo\nthree"
    assert len(reflection._clean_playbook("\n".join(f"{n}. step" for n in range(20))).splitlines()) == 8
    assert reflection._slug("Reconcile: a Statement!") == "reconcile-a-statement"
