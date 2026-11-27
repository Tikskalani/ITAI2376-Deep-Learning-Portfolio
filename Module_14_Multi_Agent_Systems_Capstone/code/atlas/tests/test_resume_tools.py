"""optimize_resume and revise_with_answers, end to end on shadowed tables with a fake model.

The draft is checked and saved, the engine's questions land in candidate_answers
with one notification, a revision reads only answers typed in the Jobs tab, and a
claim that rests on an answer passes precheck labelled "from your answer" until it
is promoted into the master. No test reaches a real model.
"""

from __future__ import annotations

import fnmatch
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agents.career.tools import build_tools
from core import db, llm
from core.jobs import answers, precheck, store
from core.jobs import config as jobs_config
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, one_page, tailor
from tests import livedb
from tests.test_resume_bridge import CONFIG, FakeModel
from tests.test_resume_format import FORMAT_TEXT

TABLES = ("job_postings", "job_applications", "job_events", "candidate_answers", "llm_calls", "episodes",
          "job_queue")
POSTING = ("We are hiring an AI Engineer to build Claude API agents with Postgres, evals and CI. "
           "Kubernetes experience is a plus.")


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    async def refuse(**_: Any) -> str:
        raise AssertionError("a test reached the real llm.complete")

    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: (_ for _ in ()).throw(AssertionError("real client")))


@pytest.fixture
async def world(monkeypatch, tmp_path):
    """Shadowed tables, a master in a temp data/resume, one posting and its application,
    a recorded notify, and the engine's run on the fake model by default."""
    (tmp_path / "master.md").write_text(FORMAT_TEXT, encoding="utf-8")
    monkeypatch.setattr(jobs_config, "RESUME_DIR", tmp_path)
    sent: list[tuple[str, str]] = []

    async def notify(kind: str, title: str, body: str = "", **_: Any) -> None:
        sent.append((kind, title))

    monkeypatch.setattr("core.surfaces.notify.notify", notify)
    async with livedb.shadow(monkeypatch, *TABLES):
        pid, _ = await store.upsert_posting({
            "source": "greenhouse", "board": "acme", "external_id": "1", "company": "Acme AI",
            "title": "AI Engineer", "location": "Remote", "remote": True, "url": "https://example.com/1",
            "salary_min": None, "salary_max": None, "posted_at": None, "description": POSTING})
        app = await store.upsert_application("Acme AI", "AI Engineer", status="interested", posting_id=pid)
        yield {"app_id": str(app["id"]), "dir": tmp_path, "sent": sent, "monkeypatch": monkeypatch}


def scripted() -> FakeModel:
    """Every stage, including keywords. A revision (an ANSWER block in the prompt) writes
    the number from Trilok's answer into B1."""

    def reply(stage: str, user: str) -> str:
        if stage == "keywords":
            return json.dumps({"requirements": [{"name": "Claude API", "shown": True},
                                                {"name": "Postgres", "shown": True},
                                                {"name": "Kubernetes", "shown": False},
                                                {"name": "evals", "shown": "yes"}]})  # not True: unsupported
        if stage == "diagnose":
            return json.dumps({"blocks": [
                {"id": "B1", "impact": 2, "specificity": 2, "scannability": 3, "relevance": 5, "issues": [],
                 "question": "How many tickets did the agent close each week?"}],
                "document": {"problems": [], "strengths": [], "summary_verdict": "", "most_relevant": ["B1"]}})
        if stage == "rewrite":
            if "<<<ANSWER" in user:
                lines = ["Built a Claude API support agent used by 40 staff that closes 120 tickets a week."]
            else:
                lines = ["Built a Claude API support agent used by 40 staff across three offices."]
            return json.dumps({"edits": [{"id": "B1", "lines": lines, "why": "sharper", "keywords": []}],
                               "order": []})
        if stage == "audit":
            items = json.loads(user.split("ITEMS:")[1].split("Return ONLY JSON")[0])
            return json.dumps({"results": [{"id": it["id"], "ok": True, "problems": []} for it in items]})
        if stage == "judge":
            return json.dumps({"A": {"score": 60}, "B": {"score": 70}, "better": "B", "why": "x"})
        if stage == "cut":
            return json.dumps({"cuts": []})
        raise AssertionError(f"unexpected stage {stage}")

    return FakeModel(reply)


def _fake_run(world: dict[str, Any], model: FakeModel) -> None:
    """Route tailor's runs through the fake model and the test config."""
    real_opt, real_rev = tailor.runner.optimize, tailor.runner.revise

    async def opt(*a: Any, **k: Any):
        return await real_opt(*a, **dict(k, config=CONFIG, complete=model))

    async def rev(*a: Any, **k: Any):
        return await real_rev(*a, **dict(k, config=CONFIG, complete=model))

    world["monkeypatch"].setattr(tailor.runner, "optimize", opt)
    world["monkeypatch"].setattr(tailor.runner, "revise", rev)


def _tool(name: str):
    return {t.name: t for t in build_tools()}[name]


async def test_optimize_resume_saves_a_checked_draft_and_asks_once(world):
    model = scripted()
    _fake_run(world, model)
    out = await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    assert out["saved"] is True and "error" not in out, out
    assert out["questions_waiting"] == 1 and out["changed"] == 1 and "text" not in out
    assert out["gaps"] == ["Kubernetes", "evals"], "only requirements marked shown are supported"

    app = await store.get_application(world["app_id"])
    resume = app["materials"]["resume"]
    text = resume["text"]
    assert text.startswith("Changed: 1 line(s) rewritten by the evidence engine")
    assert "Built a Claude API support agent used by 40 staff across three offices." in text
    record = resume["engine"]
    assert record["input"] == "full" and record["questions"][0]["id"] == "B1"
    assert record["target"]["supported"] == ["Claude API", "Postgres"]
    assert record["meter"]["stages"]["keywords"]["calls"] == 1, "the keywords stage ran inside the run"

    row = await db.fetchrow("select question, answer, context from candidate_answers where key = $1",
                            record["questions"][0]["key"])
    assert row["answer"] is None and row["context"]["kind"] == "resume"
    assert row["context"]["application_id"] == world["app_id"] and row["context"]["block_id"] == "B1"
    assert 'resume line: "Built a support agent' in row["question"]
    assert world["sent"] == [("job_question", "1 resume question(s) for Acme AI")], "one notification per run"
    assert any(e["kind"] == "draft:resume" for e in app["events"])

    check = precheck.resume_check(text, FORMAT_TEXT)
    assert check.ok and check.from_your_answer == []


async def test_a_revision_reads_only_typed_answers_and_labels_their_claims(world):
    _fake_run(world, scripted())
    await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    out = await _tool("revise_with_answers").handler({"application_id": world["app_id"]})
    assert "no answers yet" in out["error"]

    app = await store.get_application(world["app_id"])
    key = app["materials"]["resume"]["engine"]["questions"][0]["key"]
    await answers.set_answer(key, "About 120 tickets a week.")  # what the Jobs tab does
    out = await _tool("revise_with_answers").handler({"application_id": world["app_id"]})
    assert out["saved"] is True and out["revised"] == ["B1"], out
    assert out["from_your_answer"] == ["120 (from your answer)"]

    app = await store.get_application(world["app_id"])
    text = app["materials"]["resume"]["text"]
    assert "closes 120 tickets a week" in text
    typed = await answers.resume_answers()
    full = precheck.check(company="Acme AI", title="AI Engineer", resume_text=text,
                          cover="Acme AI needs an AI Engineer; I built one.", master=FORMAT_TEXT, answers=typed)
    assert full.ok, full.problems
    assert full.from_your_answer == ["120 (from your answer)"]
    blocked = precheck.check(company="Acme AI", title="AI Engineer", resume_text=text,
                             cover="Acme AI needs an AI Engineer; I built one.", master=FORMAT_TEXT)
    assert any("120" in p for p in blocked.problems), "without his answer the number is invented"


async def test_a_changed_master_or_selection_stops_a_revision(world):
    _fake_run(world, scripted())
    await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    (world["dir"] / "master.md").write_text(FORMAT_TEXT.replace("Jo Doe", "Jo Q. Doe"), encoding="utf-8")
    out = await _tool("revise_with_answers").handler({"application_id": world["app_id"]})
    assert "changed since this draft" in out["error"]


async def test_the_one_page_master_is_what_the_engine_rewrites(world):
    model = scripted()
    _fake_run(world, model)
    ids = [b["id"] for b in one_page.bullets(FORMAT_TEXT)]
    one_page.apply(FORMAT_TEXT, [ids[1]], world["dir"])  # cut the Postgres-tests bullet
    out = await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    assert out["input"] == "one_page"
    text = (await store.get_application(world["app_id"]))["materials"]["resume"]["text"]
    assert "Wrote 300 integration tests" not in text, "the cut stays cut"
    prompts = [c for c in model.calls if c["stage"] == "rewrite"]
    assert prompts, "the engine ran"


async def test_a_refused_run_saves_nothing(world):
    _fake_run(world, scripted())
    world["monkeypatch"].setattr("core.config.settings.resume_run_usd_cap", 0.0001)
    out = await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    assert out["saved"] is False and out["refused"]
    app = await store.get_application(world["app_id"])
    assert "resume" not in app["materials"]
    assert any(e["kind"] == "draft:resume:refused" for e in app["events"])


async def test_an_application_without_a_posting_is_refused(world):
    app = await store.upsert_application("Beta", "AI Engineer", status="interested")
    out = await _tool("optimize_resume").handler({"application_id": str(app["id"])})
    assert "no posting" in out["error"]


async def test_promote_marks_an_answer_once_the_master_has_it(world, capsys):
    from scripts import resume_promote

    _fake_run(world, scripted())
    await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    key = (await store.get_application(world["app_id"]))["materials"]["resume"]["engine"]["questions"][0]["key"]
    await answers.set_answer(key, "About 120 tickets a week.")
    await _tool("revise_with_answers").handler({"application_id": world["app_id"]})

    await resume_promote.listing(FORMAT_TEXT)
    shown = capsys.readouterr().out
    assert "About 120 tickets a week." in shown and "Paste under Acme Robotics" in shown
    assert "closes 120 tickets a week" in shown

    await resume_promote.check(FORMAT_TEXT, world["dir"])
    row = await db.fetchrow("select context from candidate_answers where key = $1", key)
    assert "promoted_at" not in row["context"], "not in the master yet"
    promoted = FORMAT_TEXT.replace("across three offices.", "across three offices, closing 120 tickets a week.")
    await resume_promote.check(promoted, world["dir"])
    row = await db.fetchrow("select context from candidate_answers where key = $1", key)
    assert row["context"].get("promoted_at")


# ------------------------------------------------------------------ pure parts


def test_keywords_fail_closed():
    class Reply:
        def __init__(self, text: str) -> None:
            self.text = text

        def __call__(self, system: str, user: str, **_: Any) -> str:
            assert system == tailor.KEYWORDS_SYSTEM and "<<<POSTING" in user and "<<<EVIDENCE" in user
            return self.text

    target = tailor.base_target({"title": "AI Engineer", "description": POSTING}, [])
    for unreadable in ("not json", '{"requirements": "x"}', "{}"):
        t = tailor.read_keywords(Reply(unreadable), target, FORMAT_TEXT)  # type: ignore[arg-type]
        assert t["supported"] == [] and t["keywords"] == []
    t = tailor.read_keywords(Reply(json.dumps({"requirements": [
        {"name": "RAG", "shown": True}, {"name": "rag", "shown": True}, {"name": "Go", "shown": 1},
        {"name": "", "shown": True}]})), target, FORMAT_TEXT)  # type: ignore[arg-type]
    assert t["supported"] == ["RAG"] and t["unsupported"] == ["Go"]
    assert bridge.stage_of(tailor.KEYWORDS_SYSTEM) == "keywords"


def test_seniority_from_the_title():
    assert [tailor.seniority(t) for t in ("Senior AI Engineer", "Staff Engineer", "AI Engineer",
                                          "Software Engineer, New Grad", "Engineer I")] == [
        "senior", "staff", "mid", "entry", "entry"]


def test_precheck_only_accepts_claims_from_typed_resume_answers():
    draft = FORMAT_TEXT.replace("across three offices.", "across three offices, closing 120 tickets a week.")
    r = precheck.resume_check(draft, FORMAT_TEXT, ["How many tickets? About 120 a week."])
    assert r.ok and r.from_your_answer == ["120 (from your answer)"]
    r = precheck.resume_check(draft, FORMAT_TEXT, [])
    assert not r.ok and "120" in r.summary()


def test_no_agent_or_engine_code_writes_an_answer():
    """Answers are typed by Trilok in the Jobs tab. The only caller of set_answer
    outside answers.py is that route, so no model output becomes an answer."""
    root = Path(__file__).resolve().parents[1]
    callers = sorted(str(p.relative_to(root)).replace("\\", "/")
                     for folder in ("core", "agents", "scripts")
                     for p in (root / folder).rglob("*.py")
                     if re.search(r"\bset_answer\(", p.read_text(encoding="utf-8")))
    assert callers == ["core/jobs/answers.py", "core/web/jobs.py"]


def test_the_tools_take_only_an_application_and_need_no_approval():
    seeded = ("*submit*", "*transfer*", "*payment*", "*publish*", "*send_*", "*reply*", "*comment*", "*create_event*")
    for name in ("optimize_resume", "revise_with_answers"):
        tool = _tool(name)
        assert list(tool.input_schema["properties"]) == ["application_id"]
        assert not any(fnmatch.fnmatch(name, pat) for pat in seeded)


def test_engine_drafts_reserve_and_never_pass_the_daily_sub_cap_default():
    from core.config import settings

    assert Decimal(str(settings.resume_run_usd_cap)) == Decimal("0.40")
    assert Decimal(str(settings.resume_daily_usd_cap)) == Decimal("1.00")
    assert E.REWRITE_SYSTEM in bridge.STAGE_PROMPTS


# ------------------------------------------------------------------ the acceptance script (never run for real here)


async def test_the_acceptance_script_refuses_without_yes_or_over_the_cap(capsys):
    import argparse

    from scripts import resume_accept

    base = {"yes": False, "cap": "1.50", "save": False, "application": None, "revise": None}
    assert await resume_accept.main(argparse.Namespace(**base)) == 2
    assert "--yes" in capsys.readouterr().err
    assert await resume_accept.main(argparse.Namespace(**dict(base, yes=True, cap="2.00"))) == 2
    assert "capped at $1.50" in capsys.readouterr().err


async def test_an_acceptance_pass_scores_drafts_and_writes_its_report(world, tmp_path):
    from scripts import resume_accept

    with pytest.raises(ValueError):
        await resume_accept.accept([world["app_id"]], total_cap=Decimal("2"), save=False, out_dir=tmp_path)
    out = await resume_accept.accept([world["app_id"]], total_cap=Decimal("1.50"), save=False,
                                     out_dir=tmp_path / "run", complete=scripted(), directory=world["dir"])
    s = out["summary"]
    assert s["drafted"] == 1 and s["passed"] == 1 and s["success"] is True
    row = out["rows"][0]
    assert row["invented"] == [] and row["page_ok"] and row["questions"]
    assert row["number_questions"] == 1, "'How many tickets...' asks for his number"
    assert Decimal(s["spent_usd"]) <= Decimal("0.40")
    report = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
    assert "1 of 1 drafts pass precheck with zero invented claims" in report and "SUCCESS" in report
    app = await store.get_application(world["app_id"])
    assert "resume" not in app["materials"], "without --save nothing is stored"


async def test_a_resume_line_is_asked_once_and_its_answer_reused_across_applications(world):
    model = scripted()
    _fake_run(world, model)
    pid2, _ = await store.upsert_posting({
        "source": "greenhouse", "board": "beta", "external_id": "2", "company": "Beta AI",
        "title": "AI Engineer", "location": "Remote", "remote": True, "url": "https://example.com/2",
        "salary_min": None, "salary_max": None, "posted_at": None, "description": POSTING})
    app2 = await store.upsert_application("Beta AI", "AI Engineer", status="interested", posting_id=pid2)

    first = await _tool("optimize_resume").handler({"application_id": world["app_id"]})
    assert first["questions_waiting"] == 1
    key = (await store.get_application(world["app_id"]))["materials"]["resume"]["engine"]["questions"][0]["key"]
    assert key == tailor.line_key(E.Doc(FORMAT_TEXT).by_id["B1"]["text"])
    await answers.set_answer(key, "About 120 tickets a week.")  # typed once, in Jobs

    second = await _tool("optimize_resume").handler({"application_id": str(app2["id"])})
    assert second["questions_waiting"] == 0, "the answered line is not asked again"
    assert "closes 120 tickets a week" in (await store.get_application(str(app2["id"])))["materials"]["resume"]["text"]
    prompts = [c for c in model.calls if c["stage"] == "rewrite"]
    assert prompts, "the engine rewrote with the answer as evidence"
    rows = await db.fetch("select key from candidate_answers where context->>'kind' = 'resume'")
    assert [r["key"] for r in rows] == [key], "one question per line, whichever application asked"
    assert world["sent"] == [("job_question", "1 resume question(s) for Acme AI")]


# ------------------------------------------------------------------ the blind comparison (never run for real here)


async def test_the_comparison_refuses_without_yes_or_over_its_cap(capsys):
    import argparse

    from scripts import resume_compare

    base = {"yes": False, "cap": "0.25", "run": None, "dir": None}
    assert await resume_compare.main(argparse.Namespace(**base)) == 2
    assert await resume_compare.main(argparse.Namespace(**dict(base, yes=True, cap="0.30"))) == 2
    assert "capped at $0.25" in capsys.readouterr().err


async def test_the_comparison_judges_both_orders_blind_inside_its_cap(world):
    from scripts import resume_compare

    seen: list[str] = []

    async def judge(*, user, on_usage, tier, provider, purpose, **_):
        seen.append(user)
        on_usage("m", {}, Decimal("0.01"))
        return json.dumps({"A": {"score": 60}, "B": {"score": 70}, "better": "B", "why": "x"})

    items = [{"application_id": world["app_id"], "company": "Acme AI", "role": "AI Engineer", "posting_id": None,
              "agent": resume_compare.printed("Changed: notes\n" + FORMAT_TEXT), "engine": FORMAT_TEXT + "\n"}]
    with pytest.raises(ValueError):
        await resume_compare.compare(items, total_cap=Decimal("0.30"), complete=judge)
    out = await resume_compare.compare(items, total_cap=Decimal("0.25"), complete=judge)
    assert out["judged"] == 1 and Decimal(out["spent_usd"]) <= Decimal("0.25")
    assert len(seen) == 2, "both orderings"
    assert all("Changed: notes" not in u for u in seen), "the judge sees only the printed resume"
    assert out["rows"][0]["winner"] == "tie", "each wins once as B: the averages tie"
