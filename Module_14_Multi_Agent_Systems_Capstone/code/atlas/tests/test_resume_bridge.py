"""The resume engine's model bridge: routing, the hard cap under six threads, fallbacks,
the page budget, and whole engine runs on scripted replies.

No test here reaches a real model: every run gets a fake `complete`, and an autouse
guard makes the real llm.complete and the Anthropic client raise if anything falls
through to them.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest

from core import costs, llm
from core.config import settings
from core.resume_engine import apex_domains, bridge, one_page, runner
from core.resume_engine import apex_engine as E
from tests import livedb
from tests.test_resume_format import FORMAT_TEXT


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    async def refuse(**_: Any) -> str:
        raise AssertionError("a test reached the real llm.complete")

    def no_client() -> Any:
        raise AssertionError("a test reached the real Anthropic client")

    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr("core.runtime.agent.get_client", no_client)


CONFIG = bridge.EngineConfig(
    stages={s: bridge.Route(tier="scan") for s in ("diagnose", "keywords", "cut", "audit")}
    | {s: bridge.Route(tier="specialist") for s in ("rewrite", "repair", "judge")},
    page_words=580,
    thinking_headroom_tokens=8000,
)


class FakeModel:
    """A stand-in for llm.complete. reply(stage, user) gives the text; the cost
    booked is `share` of the call's worst case, so it is always a real price."""

    def __init__(self, reply: Callable[[str, str], str], *, share: float = 0.05,
                 fail: Callable[[str, str], bool] | None = None, delay: float = 0.0) -> None:
        self.reply, self.share, self.fail, self.delay = reply, share, fail, delay
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    async def __call__(self, *, system, user, tier, provider, max_tokens, purpose, effort, on_usage,
                       cache_prefix=None):
        stage = purpose.removeprefix(bridge.PURPOSE_PREFIX)
        with self._lock:
            self.calls.append({"stage": stage, "tier": tier, "max_tokens": max_tokens, "effort": effort,
                               "cache_prefix": cache_prefix, "start": time.monotonic()})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail and self.fail(stage, tier):
                return ""  # no on_usage: the call got no answer
            with self._lock:
                self.calls[-1]["end"] = time.monotonic()
            model = llm.resolve_model(tier, provider=provider)
            worst = bridge.worst_case_usd(model, system, user, max_tokens)
            on_usage(model, {"input_tokens": 1}, (worst * Decimal(str(self.share))).quantize(Decimal("0.0001")))
            return self.reply(stage, user)
        finally:
            with self._lock:
                self.in_flight -= 1


# ------------------------------------------------------------------ routing and config


def test_every_engine_prompt_has_a_stage():
    assert {bridge.stage_of(p) for p in (E.DIAGNOSE_SYSTEM, E.REWRITE_SYSTEM, E.VERIFY_SYSTEM,
                                          E.REPAIR_SYSTEM, E.JUDGE_SYSTEM, E.CUT_SYSTEM)} == {
        "diagnose", "rewrite", "audit", "repair", "judge", "cut"}
    assert bridge.stage_of("something new") == "other"


def test_the_shipped_config_routes_cheap_stages_to_haiku_and_writing_to_sonnet():
    cfg = bridge.load_config()
    assert {s: cfg.route(s).tier for s in ("diagnose", "keywords", "cut", "audit", "other")} == dict.fromkeys(
        ("diagnose", "keywords", "cut", "audit", "other"), "scan")
    assert {s: cfg.route(s).tier for s in ("rewrite", "repair", "judge")} == dict.fromkeys(
        ("rewrite", "repair", "judge"), "specialist")
    assert all(cfg.route(s).provider == "anthropic" for s in cfg.stages)
    assert cfg.fallback_tier == "scan" and cfg.page_words == 580


def test_the_worst_case_bounds_any_real_call():
    model = settings.model_for_tier("specialist")
    system, user = "S" * 2000, "naïve résumé text " * 500
    worst = bridge.worst_case_usd(model, system, user, 15000)
    # A token is at least one byte, and the output never passes max_tokens.
    real = costs.cost_of_usage(model, {"input_tokens": len((system + user).encode()) // 3,
                                       "output_tokens": 15000})
    assert real <= worst


# ------------------------------------------------------------------ the cap under six threads


def _six(fn: Callable[[int], Any]) -> list[Any]:
    """Run fn(0..5) on six threads that start together; exceptions are returned."""
    gate = threading.Barrier(6)

    def go(i: int) -> Any:
        gate.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 - the test inspects them
            return exc

    with ThreadPoolExecutor(6) as pool:
        return list(pool.map(go, range(6)))


def test_six_reservations_at_the_cap_never_pass_it():
    w = Decimal("0.10")
    budget = bridge.RunBudget(Decimal("0.35"))

    def call(i: int) -> str:
        amount = budget.reserve(w, "rewrite")
        time.sleep(0.02)
        budget.settle(amount, w, "rewrite")  # each call costs its whole worst case
        return "ok"

    out = _six(call)
    assert out.count("ok") == 3
    assert all(isinstance(x, bridge.BudgetExceeded) for x in out if x != "ok")
    assert budget.spent == Decimal("0.30") and budget.spent <= budget.cap
    assert budget.peak <= budget.cap


def test_calls_that_fit_once_others_settle_wait_instead_of_failing():
    w = Decimal("0.10")
    budget = bridge.RunBudget(Decimal("0.35"))
    active, most = [0], [0]
    lock = threading.Lock()

    def call(i: int) -> str:
        amount = budget.reserve(w, "audit")
        with lock:
            active[0] += 1
            most[0] = max(most[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        budget.settle(amount, w / 2, "audit")  # each call costs half its worst case
        return "ok"

    assert _six(call) == ["ok"] * 6
    assert budget.spent == Decimal("0.30") and budget.peak <= budget.cap
    assert most[0] <= 3, "never more in flight than the cap can cover at worst"


def test_random_interleavings_keep_spend_under_the_cap():
    rng = random.Random(7)
    plans = [[(Decimal(rng.randint(1, 40)) / 1000, rng.random()) for _ in range(25)] for _ in range(6)]
    budget = bridge.RunBudget(Decimal("0.50"))

    def call(i: int) -> int:
        done = 0
        for amount, share in plans[i]:
            try:
                r = budget.reserve(amount, "audit")
            except bridge.BudgetExceeded:
                break
            time.sleep(rng.random() / 500)
            budget.settle(r, (amount * Decimal(str(share))).quantize(Decimal("0.0001")), "audit")
            done += 1
        return done

    _six(call)
    assert budget.spent <= budget.cap and budget.peak <= budget.cap


async def test_six_engine_threads_hit_the_cap_and_the_total_never_passes_it():
    """The real path: six engine threads, run_coroutine_threadsafe onto this loop,
    reservations under the lock, the model booking its full worst case."""
    model = FakeModel(lambda stage, user: "{}", share=1.0, delay=0.02)
    user = "x" * 4000
    w = bridge.worst_case_usd(settings.model_for_tier("specialist"), E.REWRITE_SYSTEM, user,
                              7000 + CONFIG.thinking_headroom_tokens)
    budget = bridge.RunBudget(w * Decimal("3.5"))
    engine_llm = bridge.EngineLLM(asyncio.get_running_loop(), budget, CONFIG, model)

    out = await asyncio.to_thread(_six, lambda i: engine_llm(E.REWRITE_SYSTEM, user, max_tokens=7000,
                                                             json_mode=True, effort="medium"))
    assert out.count("{}") == 3
    assert sum(isinstance(x, bridge.BudgetExceeded) for x in out) == 3
    assert len(model.calls) == 3, "a call that could pass the cap is never sent"
    assert budget.spent == sum(c.cost_usd for c in engine_llm.calls) <= budget.cap
    assert budget.peak <= budget.cap
    assert {c["tier"] for c in model.calls} == {"specialist"}
    assert {c["max_tokens"] for c in model.calls} == {7000 + CONFIG.thinking_headroom_tokens}


async def test_a_stopped_run_stops_every_thread():
    model = FakeModel(lambda stage, user: "{}", share=1.0, delay=0.01)
    budget = bridge.RunBudget(Decimal("0.0001"))
    engine_llm = bridge.EngineLLM(asyncio.get_running_loop(), budget, CONFIG, model)
    out = await asyncio.to_thread(_six, lambda i: engine_llm(E.JUDGE_SYSTEM, "x", max_tokens=1200))
    assert all(isinstance(x, bridge.BudgetExceeded) for x in out)
    assert model.calls == [] and budget.spent == 0


# ------------------------------------------------------------------ fallbacks


async def test_a_failed_stage_falls_back_to_haiku_and_is_named():
    model = FakeModel(lambda stage, user: '{"ok": 1}', fail=lambda stage, tier: tier == "specialist")
    budget = bridge.RunBudget(Decimal("1.00"))
    engine_llm = bridge.EngineLLM(asyncio.get_running_loop(), budget, CONFIG, model)
    text = await asyncio.to_thread(engine_llm, E.REWRITE_SYSTEM, "rewrite this", max_tokens=7000)
    assert text == '{"ok": 1}'
    assert [c["tier"] for c in model.calls] == ["specialist", "scan"]
    assert engine_llm.fallbacks == [{"stage": "rewrite", "from": "anthropic/specialist",
                                     "to": "anthropic/scan", "reason": engine_llm.fallbacks[0]["reason"]}]
    assert [(c.stage, c.answered, c.fallback) for c in engine_llm.calls] == [
        ("rewrite", False, False), ("rewrite", True, True)]
    assert budget.reserved == 0 and budget.spent == engine_llm.calls[1].cost_usd


async def test_a_stage_that_fails_twice_stops_the_run():
    model = FakeModel(lambda stage, user: "{}", fail=lambda stage, tier: True)

    def go(engine_llm: bridge.EngineLLM) -> str:
        return engine_llm(E.DIAGNOSE_SYSTEM, "x", max_tokens=5000)

    out, meter = await bridge.call_engine(go, cap_usd=Decimal("1.00"), config=CONFIG, complete=model)
    assert out is None and "failed twice" in (meter.stopped or "")
    assert meter.spent_usd == 0 and len(meter.fallbacks) == 1


# ------------------------------------------------------------------ page budget


def test_the_page_budget_is_one_page_for_every_role_and_comes_back():
    before = (dict(apex_domains.PAGES), apex_domains.WORDS_PER_PAGE)
    with bridge.page_budget(580):
        assert apex_domains.page_budget("ai_swe") == (1, 580)
        assert apex_domains.page_budget(None) == (1, 580)
        with bridge.page_budget(580):
            assert apex_domains.page_budget("ai_swe") == (1, 580)
        with pytest.raises(RuntimeError):
            with bridge.page_budget(700):
                pass
        assert apex_domains.page_budget(None) == (1, 580)
    assert (dict(apex_domains.PAGES), apex_domains.WORDS_PER_PAGE) == before
    assert apex_domains.page_budget(None) == (None, None)


# ------------------------------------------------------------------ whole runs


def scripted(edits: dict[str, list[str]], *, cuts: list[str] | None = None, questions: bool = True):
    """Replies for every engine stage on FORMAT_TEXT-shaped resumes."""

    def reply(stage: str, user: str) -> str:
        if stage == "diagnose":
            return json.dumps({"blocks": [
                {"id": "B1", "impact": 2, "specificity": 2, "scannability": 3, "relevance": 5,
                 "issues": ["no outcome"], "question": "How many tickets did the agent close a week?" if questions else None}],
                "document": {"problems": [], "strengths": ["agents"], "summary_verdict": "fine",
                             "most_relevant": ["B1"]}})
        if stage == "rewrite":
            return json.dumps({"edits": [{"id": k, "lines": v, "why": "sharper", "keywords": []}
                                         for k, v in edits.items()], "order": []})
        if stage == "audit":
            items = json.loads(user.split("ITEMS:")[1].split("Return ONLY JSON")[0])
            return json.dumps({"results": [{"id": it["id"], "ok": True, "problems": []} for it in items]})
        if stage == "judge":
            return json.dumps({"A": {"score": 60}, "B": {"score": 70}, "better": "B", "why": "clearer"})
        if stage == "cut":
            return json.dumps({"cuts": [{"id": i, "why": "least relevant"} for i in cuts or []]})
        raise AssertionError(f"unexpected stage {stage}")

    return reply


@pytest.fixture
def fixed_cap(monkeypatch):
    async def cap(cap_usd=None, *, daily_cap_usd=None):
        return Decimal(str(cap_usd or "0.40")), "the run cap"

    monkeypatch.setattr(bridge, "run_cap", cap)


TARGET = {"title": "AI Engineer", "seniority": "mid", "sector": "software", "domain": "ai_swe",
          "supported": ["Claude API", "Postgres"], "unsupported": ["Kubernetes"], "keywords": []}


async def test_a_whole_run_comes_back_as_a_checked_one_page_draft(fixed_cap):
    model = FakeModel(scripted({"B1": ["Built a Claude API support agent used by 40 staff across three offices."]}))
    draft = await runner.optimize(FORMAT_TEXT, TARGET, config=CONFIG, complete=model)
    assert draft.refused is None, draft.refused
    assert draft.text and "Built a Claude API support agent used by 40 staff" in draft.text
    assert draft.text.startswith("# Jo Doe") and "### Acme Robotics, Austin, TX | 2024 to present" in draft.text
    assert draft.result and draft.result["questions"][0]["id"] == "B1"
    meter = draft.meter
    assert meter and meter.spent_usd == sum(c.cost_usd for c in meter.calls) <= meter.cap_usd
    by_stage = {c.stage: c.tier for c in meter.calls}
    assert by_stage["diagnose"] == by_stage["audit"] == "scan"
    assert by_stage["rewrite"] == by_stage["judge"] == "specialist"
    assert meter.summary()["stages"]["rewrite"]["calls"] == 1


async def test_an_invented_number_never_reaches_the_draft(fixed_cap):
    model = FakeModel(scripted({"B1": ["Built a support agent used by 400 staff across nine offices."]}))
    draft = await runner.optimize(FORMAT_TEXT, TARGET, config=CONFIG, complete=model)
    assert draft.text and "400 staff" not in draft.text
    assert any(r["id"] == "B1" for r in draft.result["rejected"])


async def test_a_draft_past_one_page_is_refused_with_the_reason(fixed_cap):
    filler = "\n".join(f"- Shipped release {n} of the routing service with tests, docs and a rollback plan "
                       f"that the on-call team rehearsed before every deploy window." for n in range(60))
    long = FORMAT_TEXT.replace("- Automated invoice matching", filler + "\n- Automated invoice matching")
    model = FakeModel(scripted({}, cuts=[]))
    draft = await runner.optimize(long, TARGET, config=CONFIG, complete=model)
    assert draft.text is None
    assert draft.refused and "pages even at" in draft.refused and "one page holds about 580" in draft.refused
    assert any(c.stage == "cut" for c in draft.meter.calls), "the engine tried to cut to the page first"


async def test_a_run_at_the_cap_saves_nothing(fixed_cap):
    model = FakeModel(scripted({"B1": ["Built a Claude API support agent."]}), share=1.0)
    draft = await runner.optimize(FORMAT_TEXT, TARGET, cap_usd=Decimal("0.02"), config=CONFIG, complete=model)
    assert draft.text is None and "stopped" in (draft.refused or "")
    assert draft.meter.spent_usd <= Decimal("0.02")


async def test_a_resume_the_engine_would_misread_is_refused_before_any_call(fixed_cap):
    model = FakeModel(scripted({}))
    bad = FORMAT_TEXT.replace("### DocChat, RAG over PDFs | 2025", "### Projects")
    draft = await runner.optimize(bad, TARGET, config=CONFIG, complete=model)
    assert draft.text is None and "cannot go through the engine" in (draft.refused or "")
    assert model.calls == []


# ------------------------------------------------------------------ evidence from the full master


FULL = FORMAT_TEXT.replace(
    "- Wrote 300 integration tests against a real Postgres and ran them in CI.",
    "- Wrote 300 integration tests against a real Postgres and ran them in CI.\n"
    "- Cut ticket handling time from 9 minutes to 4 with the agent's triage step.")
ONE_PAGE = FULL.replace("- Cut ticket handling time from 9 minutes to 4 with the agent's triage step.\n", "")


@pytest.mark.parametrize("with_evidence", [True, False])
async def test_cut_lines_back_the_one_page_rewrite_only_as_evidence(fixed_cap, with_evidence):
    """The one-page text is what the engine rewrites; the lines it cut from the full
    master still count as evidence for their own role, and only through target facts."""
    claim = "Built a Claude API support agent used by 40 staff that cut ticket handling from 9 minutes to 4."
    model = FakeModel(scripted({"B1": [claim]}))
    facts = one_page.evidence(FULL, ONE_PAGE)
    assert facts == [{"topic": "Acme Robotics, Austin, TX",
                      "text": "Cut ticket handling time from 9 minutes to 4 with the agent's triage step."}]
    target = dict(TARGET, facts=facts if with_evidence else [])
    draft = await runner.optimize(ONE_PAGE, target, config=CONFIG, complete=model)
    assert draft.text is not None
    assert (claim in draft.text) is with_evidence
    assert "Cut ticket handling time" not in draft.text, "the cut line itself stays cut"


# ------------------------------------------------------------------ the one-page master


async def test_the_cut_stage_proposes_and_trilok_chooses(tmp_path):
    filler = "\n".join(f"- Ran migration batch {n} for the billing service and reconciled every account "
                       f"against the ledger before sign-off by finance." for n in range(40))
    full = FORMAT_TEXT.replace("- Automated invoice matching", filler + "\n- Automated invoice matching")
    ids = [b["id"] for b in one_page.bullets(full)]
    model = FakeModel(scripted({}, cuts=ids[3:30]))
    prop = await one_page.propose(full, config=CONFIG, complete=model)
    assert prop["proposed"] and set(prop["proposed"]) <= set(ids[3:30])
    assert [c["stage"] for c in model.calls] == ["cut"] and model.calls[0]["tier"] == "scan"
    assert prop["master_sha256"] == one_page.master_hash(full)

    mine = ids[2:40]  # not the proposal: his own choice
    text = one_page.apply(full, mine, tmp_path)
    assert one_page.load(full, tmp_path) == text
    saved = json.loads((tmp_path / one_page.SELECTION_NAME).read_text(encoding="utf-8"))
    assert [c["id"] for c in saved["cut"]] == mine and "# Jo Doe" not in json.dumps(saved), \
        "the selection holds cuts, not a second copy of the resume"


def test_a_stale_or_unsafe_selection_is_refused(tmp_path):
    ids = [b["id"] for b in one_page.bullets(FORMAT_TEXT)]
    one_page.apply(FORMAT_TEXT, [ids[1]], tmp_path)
    with pytest.raises(one_page.OnePageError, match="master resume changed"):
        one_page.load(FORMAT_TEXT.replace("Jo Doe", "Jo Q. Doe"), tmp_path)
    with pytest.raises(one_page.OnePageError, match="at least one bullet"):
        one_page.apply(FORMAT_TEXT, [ids[2]], tmp_path)  # Beta Logistics has one bullet
    with pytest.raises(one_page.OnePageError, match="not bullet ids"):
        one_page.apply(FORMAT_TEXT, ["K1"], tmp_path)
    assert one_page.load(FORMAT_TEXT, tmp_path / "missing") is None


def test_long_cut_lines_are_split_under_the_fact_limit():
    long = "First claim with 12 users. " * 40
    full = FORMAT_TEXT.replace("- Automated invoice matching for 1,200 vendors with a Python service.",
                               "- Automated invoice matching for 1,200 vendors with a Python service.\n- " + long.strip())
    facts = one_page.evidence(full, FORMAT_TEXT)
    assert len(facts) > 1 and all(len(f["text"]) <= 600 for f in facts)
    assert {f["topic"] for f in facts} == {"Beta Logistics, Austin, TX"}


# ------------------------------------------------------------------ caps from the database


async def test_the_run_cap_is_the_tightest_of_three_limits(monkeypatch):
    async with livedb.shadow(monkeypatch, "llm_calls", "episodes"):
        monkeypatch.setattr(settings, "resume_run_usd_cap", 0.40)
        monkeypatch.setattr(settings, "resume_daily_usd_cap", 1.00)
        monkeypatch.setattr(settings, "daily_usd_cap", 8.00)
        assert await bridge.run_cap() == (Decimal("0.40"), "the run cap")

        haiku = settings.model_for_tier("scan")
        per_million = costs.MODEL_PRICES[costs.model_family(haiku)]["output"]
        tokens = int(Decimal("0.75") / per_million * 1_000_000)
        await costs.record_llm_call("resume_engine/rewrite", haiku, {"output_tokens": tokens})
        assert await bridge.resume_spend_today() == Decimal("0.7500")
        cap, why = await bridge.run_cap()
        assert cap == Decimal("0.25") and "resume-engine cap" in why

        await costs.record_llm_call("triage", haiku, {"output_tokens": tokens * 9})
        monkeypatch.setattr(settings, "daily_usd_cap", 7.60)
        cap, why = await bridge.run_cap()
        assert cap == Decimal("0.10") and "Atlas cap" in why

        monkeypatch.setattr(settings, "daily_usd_cap", 7.00)
        with pytest.raises(costs.CostCapExceeded):
            await bridge.run_cap()


async def test_llm_complete_reports_usage_only_when_the_model_answered(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.undo()  # this test drives the real llm.complete with a fake client
    seen: list[tuple[str, dict, Decimal]] = []

    class Messages:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def create(self, **kw):
            if self.fail:
                raise RuntimeError("network down")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                                   usage=SimpleNamespace(input_tokens=1000, output_tokens=500))

    async with livedb.shadow(monkeypatch, "llm_calls"):
        for fail in (False, True):
            monkeypatch.setattr("core.runtime.agent.get_client",
                                lambda fail=fail: SimpleNamespace(messages=Messages(fail)))
            out = await llm.complete(system="s", user="u", tier="scan", purpose="resume_engine/test",
                                     on_usage=lambda m, u, c: seen.append((m, u, c)))
            assert out == ("" if fail else "{}")
    assert len(seen) == 1
    model, usage, cost = seen[0]
    assert usage == {"input_tokens": 1000, "output_tokens": 500}
    assert cost == costs.cost_of_usage(model, usage) > 0
