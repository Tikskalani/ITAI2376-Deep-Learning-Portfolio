"""What an agent run leaves in episodes: the ask, the answer, a verdict."""

from __future__ import annotations

from core.runtime.agent import _run_record


def test_answered_run_records_the_answer_not_just_the_prompt():
    r = _run_record("ceo", "Plan my day", "Call Dr. Will at 10, then the HVAC portal.", "end_turn", 2, 3)
    assert r["status"] == "ok"
    assert r["summary"] == "ceo: Plan my day -> Call Dr. Will at 10, then the HVAC portal."
    assert r["result"].startswith("Call Dr. Will")
    assert r["outcome"] == "answered; 2 turns, 3 tool calls"


def test_turn_limit_is_incomplete_and_says_so():
    r = _run_record("cto", "Audit the repo", "", "max_turns", 12, 30)
    assert r["status"] == "incomplete" and r["result"] is None
    assert r["summary"].endswith("no answer (max_turns)")
    partial = _run_record("cto", "Audit", "Half done", "max_tokens", 1, 0)
    assert partial["status"] == "incomplete" and "partial answer" in partial["outcome"]


def test_long_text_is_trimmed_and_whitespace_collapsed():
    r = _run_record("ceo", "q\n\n" + "x" * 500, "y " * 3000, "end_turn", 1, 1)
    assert len(r["summary"]) < 700 and "\n" not in r["summary"]
    assert len(r["result"]) <= 2000
