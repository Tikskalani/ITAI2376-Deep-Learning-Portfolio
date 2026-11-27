"""The Atlas Standard scorecard (core/standard.py and its routes).

Live test database with every table the scorecard reads as a private copy
(tests/livedb.py): Trilok's real actions, approvals and applications are
never counted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from core import db, standard
from core.jobs import store
from core.runtime import policy
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")

TABLES = ("actions", "approvals", "tool_ledger", "job_events", "job_applications", "approval_policy",
          "connector_state", "episodes", "llm_calls", "system_heartbeats", "episode_feedback", "eval_candidates")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private_tables(monkeypatch):
    policy.invalidate_cache()
    async with livedb.shadow(monkeypatch, *TABLES):
        yield
    policy.invalidate_cache()


async def _action(tool: str, approved_by: str) -> None:
    await db.execute(
        "insert into actions (agent_name, action_type, inputs, outputs, approved_by) values ('career', 'tool_call', $1, $2, $3)",
        {"tool": tool, "input": {}}, {"result": "ok"}, approved_by,
    )


@aio
async def test_a_fresh_system_is_honest_about_what_it_cannot_measure():
    s = await standard.scorecard()
    assert s["started"] is None and s["day"] is None and s["days"] == 14
    rows = {r["key"]: r for r in s["rows"]}
    assert list(rows) == ["overnight_yield", "leverage", "zero_misses", "trust", "outcomes", "cost", "learning",
                          "already_done"]
    for key in ("overnight_yield", "leverage", "zero_misses", "already_done"):
        assert rows[key]["status"] == "none" and rows[key]["value"] == "not yet"
    assert "connect.bat" in rows["zero_misses"]["detail"]
    assert rows["trust"] == {**rows["trust"], "status": "pass", "value": "0 unapproved"}
    assert "0 gated calls" in rows["trust"]["detail"] and "not tracked yet" in rows["trust"]["detail"]
    assert rows["outcomes"]["status"] == "none" and rows["outcomes"]["value"] == "0 sent"
    assert rows["cost"]["status"] == "pass" and rows["cost"]["value"] == "$0.00/day"
    assert rows["learning"]["status"] == "none" and rows["learning"]["value"].endswith(" eval cases · 0 misses waiting")
    assert int(rows["learning"]["value"].split()[0]) > 0  # the sets on disk
    assert s["measured"] == 2 and s["passing"] == 2 and s["total"] == 8


@aio
async def test_trust_counts_gated_calls_that_skipped_approval():
    await policy.set_policy("send_*", "approve")
    policy.invalidate_cache()
    await _action("send_email", "auto")   # the one that must never happen
    await _action("send_email", "user")   # went through a card
    await _action("read_resume", "auto")  # not gated: fine
    await db.execute(
        "insert into approvals (agent_name, action_summary, action_payload, status) values ('career', 'Send?', '{}', 'approved')"
    )
    await db.execute(
        "insert into tool_ledger (key, scope, tool, agent_name, status, attempts) values ('t1', 's', 'send_email', 'career', 'done', 2)"
    )
    app_row = await store.upsert_application("Testco", "AI Engineer", status="interested", source="queue")
    await store.record_event(app_row["id"], "blocked", "Pre-submit check: the cover note never names Testco")

    rows = {r["key"]: r for r in (await standard.scorecard())["rows"]}
    t = rows["trust"]
    assert t["status"] == "fail" and t["value"] == "1 unapproved"
    assert "1 gated call went through approval" in t["detail"]
    assert "1 card asked (1 yes, 0 no)" in t["detail"]
    assert "1 retry absorbed" in t["detail"]
    assert "1 invented claim caught" in t["detail"]


@aio
async def test_outcomes_need_five_applications_and_senses_show_what_is_connected():
    for i in range(4):
        await store.upsert_application(f"Testco {i}", "AI Engineer", status="applied", source="manual")
    rows = {r["key"]: r for r in (await standard.scorecard())["rows"]}
    assert rows["outcomes"]["status"] == "none" and rows["outcomes"]["value"] == "0 of 4"
    assert "Too few" in rows["outcomes"]["detail"]

    await store.upsert_application("Testco 5", "AI Engineer", status="interview", source="manual")
    await db.execute("insert into connector_state (source, cursor, last_ok_at) values ('gmail', '{}', now())")
    rows = {r["key"]: r for r in (await standard.scorecard())["rows"]}
    assert rows["outcomes"]["status"] == "pass" and rows["outcomes"]["value"] == "1 of 5"
    assert rows["zero_misses"]["status"] == "none" and "Connected: gmail" in rows["zero_misses"]["detail"]


@aio
async def test_the_fourteen_day_clock():
    assert (await standard.scorecard())["day"] is None
    await standard.start_test()
    s = await standard.scorecard()
    assert s["day"] == 1 and s["started"] is not None
    await db.execute("update system_heartbeats set last_at = now() - interval '20 days' where kind = $1",
                     standard.STARTED_KIND)
    assert (await standard.scorecard())["day"] == 14  # capped at the test length


@aio
async def test_routes():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/standard")
        assert r.status_code == 200 and len(r.json()["rows"]) == 8 and r.json()["started"] is None
        r = await c.post("/api/standard/start")
        assert r.status_code == 200 and r.json()["day"] == 1
        assert datetime.fromisoformat(r.json()["started"]).tzinfo is not None
        assert datetime.fromisoformat(r.json()["started"]) <= datetime.now(UTC)
