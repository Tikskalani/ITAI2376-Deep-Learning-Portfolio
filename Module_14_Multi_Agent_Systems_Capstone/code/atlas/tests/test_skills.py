"""Tests for the teach loop: skills, the runtime playbook block, feedback,
eval history, the learning draft and adopt path, the PWA routes, and the
MCP tools. Live test database; the model, the embedder and the notifiers
are faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, discovery, skills
from core import learning_implement as li
from core.config import settings
from core.memory.procedural import load_procedure
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Adopting and drafting write lessons and learning_* notifications under the
    # same names real ones use, and notifying pushes to every subscription;
    # those tables are private copies here, as is procedure_provenance (adopting
    # records where a procedure file came from).
    skills.invalidate_cache()
    async with livedb.shadow(monkeypatch, "lessons", "notifications", "push_subscriptions", "procedure_provenance"):
        yield
        await db.execute("delete from skills where title like 'test-%'")
        await db.execute("delete from episodes where agent_name like 'test-sk%'")
        await db.execute("delete from eval_runs where suite like 'test%'")
        await db.execute("delete from learning_candidates where source_url like 'https://test.invalid/%'")
    skills.invalidate_cache()


async def _episode(agent: str = "test-skills") -> UUID:
    return await db.fetchval(
        "insert into episodes (started_at, agent_name, summary) values (now(), $1, 'run') returning id", agent,
    )


# ---------------------------------------------------------------------------
# skills.py
# ---------------------------------------------------------------------------


async def test_add_list_block_and_status():
    a = await skills.add_skill("test-Reply in one screen", "Lead with the ask.\nOne screen, no preamble.",
                               applies_to=["client_delivery", "Client-Delivery"], domain="email",
                               provenance="user_authored")
    b = await skills.add_skill("test-Reply in one screen", "Second with the same title", applies_to=["all"],
                               provenance="user_authored")
    rows = await skills.list_skills()
    slugs = {r["slug"] for r in rows if r["id"] in (a, b)}
    assert slugs == {"test-reply-in-one-screen", "test-reply-in-one-screen-2"}
    mine = await skills.list_skills(agent="client_delivery")
    assert {r["id"] for r in mine} >= {a, b}
    assert [r["id"] for r in await skills.list_skills(agent="cfo") if r["id"] in (a, b)] == [b]

    block, ids = await skills.skills_block("client_delivery")
    assert block.startswith("Playbook: rules Trilok wrote")
    assert "- test-Reply in one screen (Trilok): Lead with the ask. One screen, no preamble." in block
    assert set(ids) >= {a, b}

    # Retiring drops it from the block right away (cache invalidated).
    assert await skills.retire_skill(a, "not useful")
    _, ids = await skills.skills_block("client_delivery")
    assert a not in ids and b in ids
    assert (await skills.get_skill(a))["retire_reason"] == "not useful"
    assert await skills.activate_skill(b)
    assert (await skills.get_skill(b))["status"] == "active"
    assert not await skills.activate_skill(uuid4())
    with pytest.raises(ValueError):
        await skills.add_skill("", "body")
    with pytest.raises(ValueError):
        await skills.add_skill("test-x", "y" * 2000)
    with pytest.raises(ValueError):
        await skills.set_status(b, "gone")
    assert skills.format_block([]) == ""
    assert await skills.remove_skill(b)


async def test_uses_feedback_and_report(monkeypatch):
    s = await skills.add_skill("test-Ask before assuming", "Ask one question when the brief is thin.",
                               applies_to=["coo"])
    e1, e2, e3 = await _episode(), await _episode(), await _episode()
    assert await skills.record_uses(e1, [s, s]) == 1
    await skills.record_uses(e2, [s])
    await skills.record_uses(e3, [s])
    assert await skills.record_uses(e3, []) == 0
    row = await skills.get_skill(s)
    assert row["uses"] == 3 and row["last_used_at"] is not None

    assert await skills.rate_episode(e1, "good") == [s]
    await skills.rate_episode(e2, "GOOD", note="nice")
    await skills.rate_episode(e3, "bad")
    await skills.rate_episode(e3, "good")  # changing a rating recounts
    row = await skills.get_skill(s)
    assert (row["good"], row["bad"]) == (3, 0)
    with pytest.raises(ValueError):
        await skills.rate_episode(e1, "meh")
    with pytest.raises(ValueError):
        await skills.rate_episode(uuid4(), "good")

    monkeypatch.setattr(settings, "skill_trial_runs", 5)
    report = [r for r in await skills.skill_report() if r["id"] == s]
    assert report and report[0]["suggestion"] == "wait" and report[0]["eval"] is None
    monkeypatch.setattr(settings, "skill_trial_runs", 2)
    report = [r for r in await skills.skill_report() if r["id"] == s]
    assert report[0]["suggestion"] == "keep"
    text = skills.format_report(report)
    assert "test-Ask before assuming [trial, coo]: used 3x, 3 good, 0 bad" in text
    assert skills.format_report([]) == ""


async def test_eval_history_feeds_the_report(monkeypatch):
    results = [SimpleNamespace(case="c1", passed=False, details=["x"]),
               SimpleNamespace(case="c2", passed=False, details=[])]
    assert await skills.record_eval_results("test_coo", results) == 2
    await db.execute("update eval_runs set created_at = now() - interval '2 days' where suite = 'test_coo'")
    s = await skills.add_skill("test-Confirm the date", "Say the date back.", applies_to=["test_coo"])
    await skills.record_eval_results("test_coo", [SimpleNamespace(case="c1", passed=True, details=[]),
                                                  SimpleNamespace(case="c2", passed=True, details=[])])
    delta = await skills.eval_delta("test_coo", datetime.now(UTC) - timedelta(days=1))
    assert delta == {"before": 0.0, "before_n": 2, "after": 1.0, "after_n": 2}
    monkeypatch.setattr(settings, "skill_trial_runs", 0)
    report = [r for r in await skills.skill_report() if r["id"] == s]
    assert report[0]["eval"]["after"] == 1.0
    assert "evals 0% -> 100%" in skills.format_report(report)


# ---------------------------------------------------------------------------
# The runtime carries the playbook and records the uses
# ---------------------------------------------------------------------------


async def test_runtime_injects_playbook_as_second_system_block():
    from core.runtime.agent import AgentConfig, run_agent
    from tests.test_runtime import _agent_env, _fake_client, _response, _text

    sid = uuid4()
    client = _fake_client([_response([_text("ok")], "end_turn")])
    cfg = AgentConfig("coo", "specialist", "system", tools=[], max_turns=2)
    with _agent_env(client), \
            patch("core.runtime.agent.skills_block", new=AsyncMock(return_value=("Learned playbook:\n- x", [sid]))), \
            patch("core.runtime.agent.record_uses", new=AsyncMock()) as rec:
        result = await run_agent(cfg, "go")
    system = client.messages.create.await_args.kwargs["system"]
    assert [b["text"] for b in system] == ["system", "Learned playbook:\n- x"]
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    rec.assert_awaited_once_with(result.episode_id, [sid])

    # No skills: no second block, nothing recorded; a failure never breaks the run.
    client = _fake_client([_response([_text("ok")], "end_turn")])
    with _agent_env(client), \
            patch("core.runtime.agent.skills_block", new=AsyncMock(side_effect=RuntimeError("db down"))), \
            patch("core.runtime.agent.record_uses", new=AsyncMock()) as rec:
        await run_agent(cfg, "go")
    assert len(client.messages.create.await_args.kwargs["system"]) == 1
    rec.assert_not_awaited()


# ---------------------------------------------------------------------------
# Draft and adopt
# ---------------------------------------------------------------------------


async def test_normalize_draft_fills_and_filters():
    d = li._normalize_draft({"kind": "weird", "applies_to": "Client-Delivery, nobody",
                             "playbook": "- do a\n* do b\n\n", "title": ""}, "The learning")
    assert d["kind"] == "playbook" and d["applies_to"] == ["ceo"]  # unknown names dropped, ceo default
    assert d["playbook"] == "do a\ndo b" and d["title"] == "The learning"
    d = li._normalize_draft({"applies_to": ["build", "all"], "playbook": ""}, "Fallback line")
    assert d["applies_to"] == ["build", "all"] and d["playbook"] == "Fallback line"


async def _candidate(learning: str = "Ask the client one clarifying question before quoting") -> UUID:
    return await discovery.create_candidate(
        learning=learning, source_type="instagram",
        source_url=f"https://test.invalid/reel/{uuid4().hex[:8]}", creator="someone",
        title="A reel", relevance=0.8, verdict="keep", rationale="useful",
    )


async def test_draft_then_adopt_creates_skill_and_procedure(tmp_path, monkeypatch):
    cid = await _candidate()
    reply = {"kind": "procedure", "applies_to": ["client_delivery"], "domain": "email",
             "slug": "clarify-before-quoting", "title": "Clarify before quoting",
             "playbook": "Ask one question first.\nQuote after the answer.",
             "body": "# Clarify\n\nSteps."}
    with patch("core.learning_implement.llm.complete_json", new=AsyncMock(return_value=reply)) as d:
        draft = await li.draft_candidate(cid)
    kw = d.await_args.kwargs
    task = kw["user"]
    assert "client_delivery, career, build" in task and "Ask the client one clarifying" in task
    assert kw["schema"] == li.DRAFT_SCHEMA and kw["tier"] == "specialist" and kw["purpose"] == "learning_draft"
    assert "CTO" in kw["system"]
    assert draft["applies_to"] == ["client_delivery"] and draft["kind"] == "procedure"
    assert (await discovery.get_candidate(cid))["status"] == "drafted"

    monkeypatch.setattr(settings, "runtime_procedures_dir", tmp_path)
    with patch("core.learning_implement.sync_from_disk", new=AsyncMock()) as sync, \
            patch("core.learning_implement.record_lesson", new=AsyncMock(return_value=uuid4())) as lesson:
        out = await li.adopt_candidate(cid)
    sync.assert_awaited_once()
    assert lesson.await_args.kwargs["domain"] == "client_delivery"
    path = tmp_path / "email" / "clarify-before-quoting.md"
    # Stored root-relative: the key the procedures table and load_procedure use.
    assert out["path"] == "email/clarify-before-quoting.md"
    assert path.read_text(encoding="utf-8").startswith("---\nname: clarify-before-quoting")
    skill = await skills.get_skill(UUID(out["skill_id"]))
    assert skill["status"] == "trial" and skill["applies_to"] == ["client_delivery"]
    assert skill["body"] == "Ask one question first.\nQuote after the answer."
    assert skill["procedure_path"] == "email/clarify-before-quoting.md"
    assert skill["source_candidate_id"] == cid
    assert await load_procedure(skill["procedure_path"]) == path.read_text(encoding="utf-8")
    assert (await discovery.get_candidate(cid))["status"] == "adopted"
    await db.execute("update skills set title = 'test-' || title where id = $1", skill["id"])
    # It came from a reel: kept as reference, labelled, and never injected.
    assert skill["provenance"] == "external_internet" and skill["source_ref"].startswith("https://test.invalid/")
    block, ids = await skills.skills_block("client_delivery")
    assert skill["id"] not in ids and "Clarify before quoting" not in block
    origin = await db.fetchrow(
        "select provenance, source_ref from procedure_provenance where root = 'runtime' and path = $1", out["path"])
    assert origin["provenance"] == "external_internet" and origin["source_ref"] == skill["source_ref"]
    await db.execute("delete from procedure_provenance where root = 'runtime' and path = $1", out["path"])
    # A second adopt of the same procedure path is refused.
    cid2 = await _candidate("Same again")
    await discovery.store_draft(cid2, draft)
    with pytest.raises(FileExistsError):
        await li.adopt_candidate(cid2)


async def test_adopt_playbook_only_and_refuses_code_note():
    cid = await _candidate("Keep replies to one screen")
    await discovery.store_draft(cid, {"kind": "playbook", "applies_to": ["all"], "title": "test-One screen",
                                      "playbook": "One screen.\nLead with the ask."})
    with patch("core.learning_implement.record_lesson", new=AsyncMock(return_value=uuid4())) as lesson:
        out = await li.adopt_candidate(cid)
    assert out["path"] is None and out["applies_to"] == ["all"]
    assert lesson.await_args.kwargs["domain"] == "news_discovery"
    assert UUID(out["skill_id"]) not in (await skills.skills_block("cfo"))[1]  # a reel: reference only

    cid = await _candidate("Needs code")
    await discovery.store_draft(cid, {"kind": "code_note", "body": "change x", "title": "test-code"})
    with pytest.raises(ValueError, match="code_note"):
        await li.adopt_candidate(cid)
    with pytest.raises(ValueError, match="no draft"):
        await li.adopt_candidate(await _candidate("undrafted"))
    with pytest.raises(ValueError, match="usable draft"):
        with patch("core.learning_implement.llm.complete_json", new=AsyncMock(return_value=None)):
            await li.draft_candidate(await _candidate("garbage reply"))


async def test_draft_and_notify_stores_a_notification():
    cid = await _candidate("Notify me")
    draft = {"kind": "playbook", "applies_to": ["ceo"], "title": "Notify me", "playbook": "Do it."}
    with patch("core.learning_implement.draft_candidate", new=AsyncMock(return_value=draft)):
        out = await li.draft_and_notify(cid)
    assert out == draft
    n = await db.fetchrow("select title, body, data from notifications where kind = 'learning_drafted' "
                          "order by created_at desc limit 1")
    assert n["title"] == "Draft ready: Notify me" and "Applies to ceo" in n["body"]
    assert n["data"]["candidate_id"] == str(cid)
    with patch("core.learning_implement.draft_candidate", new=AsyncMock(side_effect=RuntimeError("cto down"))):
        assert await li.draft_and_notify(cid) is None
    assert await db.fetchval("select count(*) from notifications where kind = 'learning_draft_failed'") >= 1


# ---------------------------------------------------------------------------
# PWA routes and MCP tools
# ---------------------------------------------------------------------------


async def test_pwa_feedback_skills_and_pursue_drafts():
    from core.web.api import app

    s = await skills.add_skill("test-Route by project", "Name the project first.", applies_to=["ceo"])
    e = await _episode()
    await skills.record_uses(e, [s])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.post(f"/api/episodes/{e}/feedback", json={"verdict": "good", "note": "spot on"})
        assert r.status_code == 200 and r.json()["skills"] == [str(s)]
        assert (await c.post(f"/api/episodes/{uuid4()}/feedback", json={"verdict": "bad"})).status_code == 404
        assert (await c.post(f"/api/episodes/{e}/feedback", json={"verdict": "meh"})).status_code == 422
        r = await c.get("/api/skills", params={"agent": "ceo"})
        mine = [k for k in r.json()["skills"] if k["id"] == str(s)]
        assert mine and mine[0]["good"] == 1 and mine[0]["uses"] == 1
        assert (await c.post(f"/api/skills/{s}/keep")).json() == {"status": "active"}
        assert (await c.post(f"/api/skills/{s}/retire", json={"reason": "no"})).json() == {"status": "retired"}
        assert (await c.post(f"/api/skills/{uuid4()}/retire")).status_code == 404
        assert (await skills.get_skill(s))["retire_reason"] == "no"

        cid = uuid4()
        with patch("core.web.pwa.transition", new=AsyncMock(return_value=True)), \
                patch("core.learning_implement.draft_in_background") as bg:
            r = await c.post(f"/api/learnings/{cid}/pursue")
        assert r.json() == {"moved": True, "drafting": True}
        bg.assert_called_once_with(cid)
        with patch("core.web.pwa.transition", new=AsyncMock(return_value=False)), \
                patch("core.learning_implement.draft_in_background") as bg:
            assert (await c.post(f"/api/learnings/{cid}/pursue")).json() == {"moved": False, "drafting": False}
        bg.assert_not_called()


async def test_mcp_rate_outcome_and_list_skills():
    from core import mcp_server as m

    s = await skills.add_skill("test-MCP skill", "Do the thing.", applies_to=["ceo"])
    e = await _episode()
    await skills.record_uses(e, [s])
    out = await m.rate_outcome(str(e), "bad", note="missed the point")
    assert out["skills_affected"] == [str(s)]
    assert (await m.rate_outcome(str(uuid4()), "good"))["error"]
    listed = [k for k in await m.list_skills(agent="ceo") if k["id"] == str(s)]
    assert listed and listed[0]["bad"] == 1 and listed[0]["status"] == "trial"
    assert not [k for k in await m.list_skills(status="retired") if k["id"] == str(s)]


# ---------------------------------------------------------------------------
# Sunday review and meta-eval
# ---------------------------------------------------------------------------


async def test_weekly_review_carries_the_skill_report():
    from core.loop import learning_digest as ld

    s = await skills.add_skill("test-Weekly skill", "Be brief.", applies_to=["coo"])
    summ = {"days": 7, "by_status": {"adopted": 1}, "adopted": [{"learning": "Be brief"}], "pending_total": 0}
    with patch.object(ld.discovery, "weekly_summary", new=AsyncMock(return_value=summ)), \
            patch.object(ld, "notify", new=AsyncMock(return_value=SimpleNamespace(id=1))) as n:
        await ld.weekly_learnings_review()
    body = n.await_args.kwargs["body"]
    assert "Playbook in force:" in body and "test-Weekly skill [trial, coo]: used 0x" in body
    assert "Keep or retire from the Playbook panel" in body
    await skills.remove_skill(s)


async def test_meta_eval_playbook_check():
    from core.meta_eval import _check_playbook_in_use

    def counts(check) -> tuple[int, int]:
        """(used, total) from the detail; real skills on a live database count too."""
        if check.detail == "no adopted skills yet":
            return 0, 0
        used, total = check.detail.split(" ", 1)[0].split("/")
        return int(used), int(total)

    used0, total0 = counts(await _check_playbook_in_use(7))
    s = await skills.add_skill("test-Old skill", "x", applies_to=["ceo"])
    await db.execute("update skills set created_at = now() - interval '3 days' where id = $1", s)
    check = await _check_playbook_in_use(7)
    assert check.name == "playbook_in_use" and not check.passed
    assert counts(check) == (used0, total0 + 1)
    await skills.record_uses(await _episode(), [s])
    check = await _check_playbook_in_use(7)
    assert counts(check) == (used0 + 1, total0 + 1)
    assert check.passed == (used0 == total0)


async def test_only_his_own_and_approved_reflection_skills_reach_a_prompt():
    mine = await skills.add_skill("test-Mine", "His own rule.", applies_to=["coo"], provenance="user_authored")
    atlas = await skills.add_skill("test-Atlas", "From a run.", applies_to=["coo"], provenance="agent_reflection")
    web = await skills.add_skill("test-Web", "From a video.", applies_to=["coo"], provenance="external_internet")
    unknown = await skills.add_skill("test-Unknown", "No idea where from.", applies_to=["coo"])
    assert (await skills.get_skill(unknown))["provenance"] == "unknown"  # never assumed to be his

    _, ids = await skills.skills_block("coo")
    assert mine in ids and not {atlas, web, unknown} & set(ids)

    # His approval lets the Atlas-written one in; it stays Atlas-written.
    row = await skills.review_skill(atlas, "approved", note="read it, fine")
    assert row["provenance"] == "agent_reflection" and row["review"] == "approved" and row["injectable"]
    block, ids = await skills.skills_block("coo")
    assert atlas in ids and "- test-Atlas (Atlas, approved by Trilok): From a run." in block

    # Approving internet-derived or unknown text records the review and injects nothing.
    for sid in (web, unknown):
        row = await skills.review_skill(sid, "approved")
        assert row["review"] == "approved" and not row["injectable"]
    _, ids = await skills.skills_block("coo")
    assert not {web, unknown} & set(ids)

    # Revoking takes it out again; both reviews stay on record.
    await skills.review_skill(atlas, "revoked")
    _, ids = await skills.skills_block("coo")
    assert atlas not in ids
    assert await db.fetchval("select count(*) from skill_reviews where skill_id = $1", atlas) == 2

    with pytest.raises(ValueError):
        await skills.review_skill(atlas, "maybe")
    with pytest.raises(LookupError):
        await skills.review_skill(uuid4(), "approved")
    with pytest.raises(ValueError):
        await skills.add_skill("test-bad", "x", provenance="mine")


async def test_provenance_is_fixed_once_known():
    atlas = await skills.add_skill("test-Fixed", "From a run.", applies_to=["coo"], provenance="agent_reflection")
    unknown = await skills.add_skill("test-Later", "Unknown at first.", applies_to=["coo"])
    # Nothing becomes his after the fact, and a known origin never changes.
    for sid, to in ((atlas, "user_authored"), (atlas, "external_internet"), (unknown, "user_authored")):
        with pytest.raises(Exception, match="provenance is fixed"):
            await db.execute("update skills set provenance = $2 where id = $1", sid, to)
    # unknown may still be resolved to a known outside origin.
    await db.execute("update skills set provenance = 'external_internet' where id = $1", unknown)
    assert (await skills.get_skill(unknown))["provenance"] == "external_internet"


async def test_candidate_source_types_map_to_provenance():
    assert skills.provenance_for_source_type("reflection") == "agent_reflection"
    assert skills.provenance_for_source_type("YouTube") == "external_internet"
    assert skills.provenance_for_source_type("instagram") == "external_internet"
    # A forwarded link is someone else's text; nothing maps to his own.
    for other in ("manual", "", None, "user"):
        assert skills.provenance_for_source_type(other) == "unknown"
