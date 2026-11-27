"""Unit tests for the agent eval harness (core/runtime/evals.py).

The dispatcher is mocked, so no real agents run and no tokens are spent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.runtime.evals import (
    EvalCase,
    expect_contains,
    expect_no_tool_errors,
    expect_tool_used,
    run_eval,
    run_suite,
)


def _result(text="", tool_calls=None):
    return SimpleNamespace(text=text, tool_calls=tool_calls or [])


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_expect_contains():
    assert expect_contains("MOQ")(_result(text="your MOQ is 50"))[0] is True
    assert expect_contains("margin")(_result(text="your MOQ is 50"))[0] is False


def test_expect_tool_used():
    res = _result(tool_calls=[{"name": "recall_facts", "is_error": False}])
    assert expect_tool_used("recall_facts")(res)[0] is True
    assert expect_tool_used("create_goal")(res)[0] is False


def test_expect_no_tool_errors():
    clean = _result(tool_calls=[{"name": "recall_facts", "is_error": False}])
    assert expect_no_tool_errors()(clean)[0] is True
    broken = _result(tool_calls=[{"name": "recall_facts", "is_error": True}])
    assert expect_no_tool_errors()(broken)[0] is False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def test_run_eval_all_checks_pass():
    case = EvalCase(
        name="ceo recalls",
        agent="ceo",
        message="what is my MOQ target?",
        checks=[expect_contains("50"), expect_tool_used("recall_facts")],
    )
    result = _result(
        text="Your MOQ target is 50 units.",
        tool_calls=[{"name": "recall_facts", "is_error": False}],
    )
    with patch(
        "core.runtime.evals.dispatch", new=AsyncMock(return_value=result)
    ):
        out = await run_eval(case)
    assert out.passed is True
    assert len(out.details) == 2


async def test_run_eval_failing_check():
    case = EvalCase(
        name="ceo recalls",
        agent="ceo",
        message="what is my MOQ target?",
        checks=[expect_contains("never appears")],
    )
    with patch(
        "core.runtime.evals.dispatch",
        new=AsyncMock(return_value=_result(text="something else")),
    ):
        out = await run_eval(case)
    assert out.passed is False


async def test_run_eval_dispatch_failure():
    case = EvalCase(name="boom", agent="ceo", message="hi", checks=[])
    with patch(
        "core.runtime.evals.dispatch",
        new=AsyncMock(side_effect=RuntimeError("api down")),
    ):
        out = await run_eval(case)
    assert out.passed is False
    assert "dispatch raised" in out.details[0]


async def test_run_suite():
    cases = [
        EvalCase(name="a", agent="ceo", message="x", checks=[]),
        EvalCase(name="b", agent="ceo", message="y", checks=[]),
    ]
    with patch(
        "core.runtime.evals.dispatch",
        new=AsyncMock(return_value=_result(text="ok")),
    ):
        results = await run_suite(cases)
    assert [r.case for r in results] == ["a", "b"]
    assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# expect_no_voice_violations
# ---------------------------------------------------------------------------


def test_voice_clean_text_passes():
    from core.runtime.evals import expect_no_voice_violations

    ok, _ = expect_no_voice_violations()(
        _result(
            text=(
                "Cache invalidation is hard because state lives in two "
                "places, and the two are never quite in agreement. The "
                "fix is usually to make one of them the source of truth."
            )
        )
    )
    assert ok is True


def test_voice_catches_em_dash():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(text="Pricing is good — delivery is fast.")
    )
    assert ok is False
    assert "em-dash" in detail


def test_voice_catches_ai_tells():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(
            text="I hope this helps! Let me know if you have any questions."
        )
    )
    assert ok is False
    assert "let me know" in detail


def test_voice_catches_forbidden_words():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(text="The system is robust and seamless.")
    )
    assert ok is False
    assert "robust" in detail and "seamless" in detail


def test_voice_catches_sentence_initial_transitions():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(
            text=(
                "The cache invalidates on write. Furthermore, the read "
                "path falls back to the database."
            )
        )
    )
    assert ok is False
    assert "Furthermore" in detail


def test_voice_allows_transition_word_mid_sentence():
    from core.runtime.evals import expect_no_voice_violations

    # "furthermore" mid-sentence is OK; only sentence-initial is banned.
    ok, _ = expect_no_voice_violations()(
        _result(text="The argument is furthermore complicated by latency.")
    )
    assert ok is True


def test_voice_catches_let_me_know_family():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(text="Let me know if you want any edits.")
    )
    assert ok is False
    assert "let me know" in detail

    ok, detail = expect_no_voice_violations()(
        _result(text="Just let me know if anything else changes.")
    )
    assert ok is False
    assert "let me know" in detail


def test_voice_catches_say_the_word():
    from core.runtime.evals import expect_no_voice_violations

    ok, detail = expect_no_voice_violations()(
        _result(text="Just say the word and I will get it done.")
    )
    assert ok is False
    assert "say the word" in detail


def test_voice_allows_tell_me_as_alternative():
    """'Tell me if X' is the clean replacement for 'let me know if X'."""
    from core.runtime.evals import expect_no_voice_violations

    ok, _ = expect_no_voice_violations()(
        _result(text="Tell me if you want any edits and I'll change it.")
    )
    assert ok is True
