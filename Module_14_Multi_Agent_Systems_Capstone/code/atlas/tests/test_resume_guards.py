"""The summary guard keeps the original summary when a rewrite collapses it, and only then."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from core import llm
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, guards, runner
from tests.test_resume_bridge import CONFIG, TARGET, FakeModel, scripted
from tests.test_resume_format import FORMAT_TEXT

SUMMARY = "AI engineer who ships tested agent systems for small businesses."


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    async def refuse(**_: Any) -> str:
        raise AssertionError("a test reached the real llm.complete")

    monkeypatch.setattr(llm, "complete", refuse)


@pytest.mark.parametrize("summary,title", [
    ("AI engineer who takes agentic applications from prototype to production.", "AI engineer"),
    ("Senior software engineer with 8 years building backend systems.", "Senior software engineer"),
    ("Data Analyst. Six years of SQL and dashboards.", "Data Analyst"),
    ("Physical therapist, licensed in Texas.", "Physical therapist"),
    ("Experienced professional looking for a new opportunity.", None),
    ("Built Trade Worker, a voice agent in pilot.", None),
    ("", None),
])
def test_the_title_a_summary_leads_with(summary, title):
    assert guards.summary_title(summary) == title


def _result(after: list[str], other: dict[str, list[str]] | None = None) -> dict[str, Any]:
    doc = E.Doc(FORMAT_TEXT)
    edits = {"S": after, **(other or {})}
    changes = [{"id": k, "after": v} for k, v in edits.items()]
    return {"final": doc.render(edits), "changes": changes, "order": [], "section_order": None}


def test_a_collapsed_summary_is_put_back_and_the_other_edits_stay():
    b1 = E.Doc(FORMAT_TEXT).by_id["B1"]["text"]
    r = _result(["Ships agents."], {"B1": [b1 + " Rewritten."]})
    reason = guards.summary_guard(FORMAT_TEXT, r)
    assert reason and "from 10 to 2 words" in reason
    assert SUMMARY in r["final"] and "Ships agents." not in r["final"]
    assert b1 + " Rewritten." in r["final"], "the guard reverts the summary only"
    assert [c["id"] for c in r["changes"]] == ["B1"] and r["summary_guard"] == reason


def test_a_dropped_title_is_put_back_even_at_full_length():
    r = _result(["Engineer of tested agent systems for small businesses, shipped end to end."])
    reason = guards.summary_guard(FORMAT_TEXT, r)
    assert reason and 'dropped your role title "AI engineer"' in reason and SUMMARY in r["final"]


def test_a_good_rewrite_passes_untouched():
    after = ["AI engineer who ships tested agent systems with evals and CI for small businesses."]
    r = _result(after)
    final = r["final"]
    assert guards.summary_guard(FORMAT_TEXT, r) is None
    assert r["final"] == final and "summary_guard" not in r


def test_no_summary_edit_or_a_created_summary_is_left_alone():
    r = {"final": FORMAT_TEXT, "changes": [], "order": [], "section_order": None}
    assert guards.summary_guard(FORMAT_TEXT, r) is None
    no_summary = FORMAT_TEXT.replace("## SUMMARY\n" + SUMMARY + "\n", "")
    doc = E.Doc(no_summary)
    created = {"final": doc.render({"S": ["Short."]}), "changes": [{"id": "S", "after": ["Short."]}],
               "order": [], "section_order": None}
    assert guards.summary_guard(no_summary, created) is None


async def test_a_run_through_the_runner_keeps_the_original_and_says_why(monkeypatch):
    async def cap(cap_usd=None, *, daily_cap_usd=None):
        return Decimal("0.40"), "the run cap"

    monkeypatch.setattr(bridge, "run_cap", cap)
    model = FakeModel(scripted({"S": ["Ships agents."]}))
    draft = await runner.optimize(FORMAT_TEXT, TARGET, config=CONFIG, complete=model)
    assert draft.text and SUMMARY in draft.text
    assert draft.result and "Kept your original summary" in draft.result["summary_guard"]
    assert draft.meter and any("Kept your original summary" in ln for ln in draft.meter.log)
    assert json.dumps(draft.result["changes"]).count('"id": "S"') == 0
