"""Prefix caching in the bridge, replayable recordings, the nightly flag, and the
acceptance report. No test reaches a real model: fakes and recordings only, with the
same autouse guard as the other resume tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import costs, llm
from core.config import settings
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, replay, runner
from core.resume_engine import resume_format as RF
from tests import livedb
from tests.test_resume_bridge import TARGET, FakeModel, _six, scripted
from tests.test_resume_format import FORMAT_TEXT, MASTER_DIR, _has_master


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    async def refuse(**_: Any) -> str:
        raise AssertionError("a test reached the real llm.complete")

    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: (_ for _ in ()).throw(AssertionError("real client")))


CACHED = bridge.EngineConfig(
    stages={"rewrite": bridge.Route(tier="specialist", cache_until="REVIEWER NOTES PER ID:"),
            "judge": bridge.Route(tier="specialist", cache_until="=== A ==="),
            "audit": bridge.Route(tier="scan"), "diagnose": bridge.Route(tier="scan"),
            "cut": bridge.Route(tier="scan")},
    page_words=580,
)


@pytest.fixture
def fixed_cap(monkeypatch):
    async def cap(cap_usd=None, *, daily_cap_usd=None):
        return Decimal(str(cap_usd or "0.40")), "the run cap"

    monkeypatch.setattr(bridge, "run_cap", cap)


# ------------------------------------------------------------------ caching


def test_the_shipped_config_caches_only_stages_with_a_shared_prefix():
    cfg = bridge.load_config()
    assert cfg.route("rewrite").cache_until == "REVIEWER NOTES PER ID:"
    assert cfg.route("judge").cache_until == "=== A ==="
    assert all(cfg.route(s).cache_until is None for s in ("diagnose", "keywords", "audit", "cut"))
    assert cfg.nightly is False


def test_the_prefix_is_the_text_before_the_marker_and_never_the_whole_prompt():
    route = CACHED.route("judge")
    assert bridge.cache_prefix(route, "posting...\n=== A ===\nresume") == "posting...\n"
    assert bridge.cache_prefix(route, "no marker here") is None
    assert bridge.cache_prefix(route, "=== A === at the start") is None
    assert bridge.cache_prefix(CACHED.route("audit"), "anything === A ===") is None


def test_a_cached_call_reserves_at_the_cache_write_rate():
    model = settings.model_for_tier("specialist")
    plain = bridge.worst_case_usd(model, "s", "u" * 9000, 1000)
    cached = bridge.worst_case_usd(model, "s", "u" * 9000, 1000, cached=True)
    assert cached > plain
    # Whatever split the API reports between write, read and plain input, the cached bound covers it.
    for usage in ({"cache_creation_input_tokens": 9000, "output_tokens": 1000},
                  {"cache_read_input_tokens": 8000, "input_tokens": 1000, "output_tokens": 1000}):
        assert costs.cost_of_usage(model, usage) <= cached


async def test_the_judges_two_orderings_run_one_after_the_other_and_share_the_prefix():
    """Six threads asking with one prefix: the first runs alone, the rest after it finished."""
    model = FakeModel(lambda stage, user: "{}", delay=0.05)
    budget = bridge.RunBudget(Decimal("5"))
    engine_llm = bridge.EngineLLM(asyncio.get_running_loop(), budget, CACHED, model)
    prompts = [f"POSTING AND RUBRIC\n=== A ===\nresume {i}" for i in range(6)]
    out = await asyncio.to_thread(_six, lambda i: engine_llm(E.JUDGE_SYSTEM, prompts[i], max_tokens=1200))
    assert out == ["{}"] * 6
    first = min(model.calls, key=lambda c: c["start"])
    rest = [c for c in model.calls if c is not first]
    assert all(c["start"] >= first["end"] for c in rest), "nobody starts before the cache is written"
    assert {c["cache_prefix"] for c in model.calls} == {"POSTING AND RUBRIC\n"}
    assert budget.spent <= budget.cap


async def test_calls_without_a_shared_prefix_still_fan_out():
    model = FakeModel(lambda stage, user: "{}", delay=0.05)
    engine_llm = bridge.EngineLLM(asyncio.get_running_loop(), bridge.RunBudget(Decimal("5")), CACHED, model)
    await asyncio.to_thread(_six, lambda i: engine_llm(E.VERIFY_SYSTEM, f"items {i}", max_tokens=3000))
    assert model.max_in_flight > 1 and {c["cache_prefix"] for c in model.calls} == {None}


async def test_llm_complete_sends_the_prefix_as_a_cached_block(monkeypatch):
    sent: list[dict[str, Any]] = []
    seen: list[dict[str, int]] = []

    class Messages:
        async def create(self, **kw):
            sent.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                                   usage=SimpleNamespace(input_tokens=200, output_tokens=50,
                                                         cache_creation_input_tokens=0,
                                                         cache_read_input_tokens=4000))

    monkeypatch.undo()
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: SimpleNamespace(messages=Messages()))
    async with livedb.shadow(monkeypatch, "llm_calls"):
        await llm.complete(system="s", user="SHARED PART tail", tier="specialist",
                           purpose="resume_engine/judge", cache_prefix="SHARED PART ",
                           on_usage=lambda m, u, c: seen.append(u))
        await llm.complete(system="s", user="no prefix here", tier="specialist",
                           purpose="resume_engine/judge", cache_prefix="SHARED PART ")
    blocks = sent[0]["messages"][0]["content"]
    assert blocks == [{"type": "text", "text": "SHARED PART ", "cache_control": {"type": "ephemeral"}},
                      {"type": "text", "text": "tail"}]
    assert sent[1]["messages"][0]["content"] == "no prefix here"
    assert seen == [{"input_tokens": 200, "output_tokens": 50, "cache_read_input_tokens": 4000}]


async def test_the_meter_reports_cache_reads_and_writes_per_stage():
    async def complete(*, on_usage, tier, provider, **kw):
        on_usage("m", {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 900,
                       "cache_creation_input_tokens": 50}, Decimal("0.001"))
        return "{}"

    def go(engine_llm: bridge.EngineLLM) -> str:
        return engine_llm(E.JUDGE_SYSTEM, "P\n=== A ===\nx", max_tokens=1200)

    _, meter = await bridge.call_engine(go, cap_usd=Decimal("1"), config=CACHED, complete=complete)
    judge = meter.summary()["stages"]["judge"]
    assert (judge["input"], judge["output"], judge["cache_read"], judge["cache_write"]) == (100, 10, 900, 50)


# ------------------------------------------------------------------ replay


async def test_a_recorded_run_replays_offline_to_the_same_draft(fixed_cap, tmp_path):
    path = tmp_path / "replies.jsonl"
    recorded = await runner.optimize(
        FORMAT_TEXT, TARGET, config=CACHED,
        complete=replay.recorder(FakeModel(scripted({"B1": ["Built a Claude API support agent used by 40 staff."]})),
                                 path))
    assert recorded.text and path.is_file()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert {x["stage"] for x in lines} >= {"diagnose", "rewrite", "audit", "judge"}

    again = replay.Replay.from_jsonl(path)
    replayed = await runner.optimize(FORMAT_TEXT, TARGET, config=CACHED, complete=again)
    assert again.misses == [] and again.hits == len(lines)
    assert replayed.text == recorded.text
    assert replayed.meter is not None and replayed.meter.spent_usd == 0


async def test_a_changed_prompt_misses_and_stops_the_replay(fixed_cap, tmp_path):
    path = tmp_path / "replies.jsonl"
    await runner.optimize(FORMAT_TEXT, TARGET, config=CACHED,
                          complete=replay.recorder(FakeModel(scripted({})), path))
    again = replay.Replay.from_jsonl(path)
    draft = await runner.optimize(FORMAT_TEXT, dict(TARGET, title="Staff AI Engineer"), config=CACHED,
                                  complete=again)
    assert again.misses and draft.text is None and "failed twice" in (draft.refused or "")


def _latest_recording() -> Path | None:
    root = Path(os.environ.get("ATLAS_RESUME_ACCEPT_DIR") or (Path(__file__).resolve().parents[1] / "data" /
                                                             "resume_accept"))
    runs = sorted(p for p in root.glob("*") if (p / "replies.jsonl").is_file() and (p / "report.json").is_file()) \
        if root.is_dir() else []
    return runs[-1] if runs else None


@pytest.mark.skipif(not (_has_master and _latest_recording()), reason="no recorded acceptance run on this machine")
async def test_the_last_acceptance_run_replays_offline(monkeypatch, tmp_path):
    """Re-run the recorded acceptance pass against its recorded replies: zero misses and
    the same drafts, at no cost. Skips when its inputs (master, postings) changed since."""
    from scripts import resume_accept

    run = _latest_recording()
    assert run is not None
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    drafted = [r for r in report["rows"] if r.get("draft")]
    async with livedb.shadow(monkeypatch, "candidate_answers", "llm_calls", "episodes"):
        rep = replay.Replay.from_jsonl(run / "replies.jsonl")
        out = await resume_accept.accept([r["application_id"] for r in drafted], total_cap=Decimal("1.50"),
                                         save=False, out_dir=tmp_path, complete=rep, directory=MASTER_DIR)
    if rep.misses:
        inputs = report["summary"].get("inputs")
        pytest.skip("the recording's inputs changed since it was made" if inputs else
                    "the recording predates input fingerprints and its inputs changed")
    assert Decimal(out["summary"]["spent_usd"]) == 0
    for new, old in zip(out["rows"], drafted, strict=True):
        assert new.get("draft"), new.get("refused")
        if not new.get("summary_guard") or old.get("summary_guard"):
            assert new["draft"] == old["draft"]
        else:
            # The guard (added after this recording) put the original summary back; nothing else moved.
            source = E.Doc(RF.master_format(MASTER_DIR))
            original = [ln for ln in source.lines[source.by_id["S"]["start"]:source.by_id["S"]["end"]] if ln.strip()]
            assert all(ln in new["draft"].split("\n") for ln in original)


# ------------------------------------------------------------------ the nightly flag


def test_the_queue_drafts_by_hand_unless_the_flag_is_on(monkeypatch):
    from core.jobs import queue

    app, posting = {"id": "a1"}, {"id": "p1", "title": "AI Engineer", "company": "Acme"}
    monkeypatch.setattr(queue, "_engine_nightly", lambda: False)
    off = queue._draft_task(app, posting)
    assert "Do not use optimize_resume" in off and "save_materials kind 'resume'" in off
    monkeypatch.setattr(queue, "_engine_nightly", lambda: True)
    on = queue._draft_task(app, posting)
    assert "with optimize_resume" in on and "Do not use" not in on


def test_an_unreadable_config_keeps_the_queue_on_hand_drafting(monkeypatch):
    from core.jobs import queue

    def broken(*_: Any) -> Any:
        raise ValueError("bad yaml")

    monkeypatch.setattr("core.resume_engine.bridge.load_config", broken)
    assert queue._engine_nightly() is False


# ------------------------------------------------------------------ the report


def test_the_console_report_never_prints_resume_text():
    from scripts import resume_accept

    secret = "UNIQUE-RESUME-LINE-42"
    row = {"company": "Acme", "role": "AI Engineer", "spent_usd": "0.12", "changed": 3, "left_as_written": 1,
           "invented": [], "page_ok": True, "other_problems": [], "from_your_answer": [], "words": 600,
           "gaps": [], "score": {"before": 50, "after": 60}, "veto": None, "fallbacks": [],
           "questions": ["How many users?"], "number_questions": 1, "draft": f"# Jo\n- {secret}",
           "existing_draft": f"# Jo\n- {secret} old", "cost": {"stages": {"judge": {
               "calls": 2, "cost_usd": "0.04", "input": 100, "output": 10, "cache_read": 900, "cache_write": 50}}},
           "existing": {"present": True, "words": 610, "invented": [], "page_ok": True, "other_problems": [],
                        "by_engine": False, "cost_usd": "0.31"}}
    summary = {"at": "now", "spent_usd": "0.12", "total_cap_usd": "1.50", "saved": False, "passed": 1,
               "drafted": 1, "with_questions": 1, "success": True,
               "nightly": {"queue_daily_cap": 5, "per_night_usd": "0.60", "per_run_usd": "0.1200",
                           "per_night_capped_usd": "0.60", "per_month_usd": "18.00"}}
    console = resume_accept.render(summary, [row], drafts=False)
    assert secret not in console
    assert "| Acme, AI Engineer | PASS | 600 | 1 | 0.12 | PASS | 610 | 0.31 |" in console
    assert "| judge | 2 | 0.04 | 100 | 10 | 900 | 50 |" in console
    assert "about $18.00 a month" in console
    assert secret in resume_accept.render(summary, [row], drafts=True)
