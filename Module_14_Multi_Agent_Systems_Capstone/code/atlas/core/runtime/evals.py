"""Atlas -- the agent eval harness.

A small harness for golden test cases against agents. Agents make real
model calls and judgment calls, so a prompt edit can silently degrade
quality; an eval suite catches that. An EvalCase pairs an input with a
set of checks; run_eval dispatches the agent and runs the checks against
the result.

Running evals spends real Claude API tokens, so it is a deliberate,
separate act. The harness itself is unit-tested with a mocked dispatcher.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.runtime.dispatcher import dispatch

log = logging.getLogger(__name__)

# A check takes an AgentResult and returns (passed, human-readable detail).
Check = Callable[[Any], "tuple[bool, str]"]


@dataclass
class EvalCase:
    """One golden test case for an agent."""

    name: str
    agent: str
    message: str
    checks: list[Check] = field(default_factory=list)


@dataclass
class EvalResult:
    """The outcome of running one EvalCase."""

    case: str
    passed: bool
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Common checks
# ---------------------------------------------------------------------------


def expect_contains(substring: str, *, case_insensitive: bool = True) -> Check:
    """Check the agent's reply contains `substring`."""

    def check(result: Any) -> tuple[bool, str]:
        text = result.text or ""
        hay = text.lower() if case_insensitive else text
        needle = substring.lower() if case_insensitive else substring
        ok = needle in hay
        return ok, f"reply contains {substring!r}: {ok}"

    return check


def expect_tool_used(tool_name: str) -> Check:
    """Check the agent called `tool_name` at least once."""

    def check(result: Any) -> tuple[bool, str]:
        used = [tc["name"] for tc in getattr(result, "tool_calls", [])]
        ok = tool_name in used
        return ok, f"used tool {tool_name!r}: {ok}"

    return check


def expect_no_tool_errors() -> Check:
    """Check no tool call returned an error."""

    def check(result: Any) -> tuple[bool, str]:
        errored = [
            tc["name"]
            for tc in getattr(result, "tool_calls", [])
            if tc.get("is_error")
        ]
        ok = not errored
        detail = f"no tool errors: {ok}"
        if errored:
            detail += f" (errored: {errored})"
        return ok, detail

    return check



# Forbidden literal substrings (lowercased) -- straight bans from the voice
# guide. Matched as substrings.
_FORBIDDEN_LITERALS = (
    "—",  # em-dash
    "it's important to note",
    "let me know",          # catches "let me know if you have any questions",
                            # "let me know if you want any edits", and
                            # "just let me know if X" alike
    "i hope this helps",
    "hope this finds you well",
    "feel free to",
    "just wanted to",
    "i wanted to reach out",
    "say the word",         # catches "just say the word"
    "delve into",
    "best regards",
    "warm regards",
    "as an ai",
)

# Words flagged with word-boundary matches. These are heuristic: "leverage"
# and "navigate" do occur legitimately, but rarely enough in HCC coursework
# that the false-positive cost is worth it.
_FORBIDDEN_WORDS = ("robust", "seamless", "leverage", "navigate")

# Transition starters: banned only when they begin a sentence.
_FORBIDDEN_TRANSITIONS = ("Moreover", "Furthermore", "Additionally")


def expect_no_voice_violations() -> Check:
    """Check the agent's reply against the voice guide's strict bans.

    Flags the em-dash, the listed AI-tell phrases, the listed banned words
    by word boundary, and Moreover/Furthermore/Additionally when used as a
    sentence-initial transition.
    """

    def check(result: Any) -> tuple[bool, str]:
        text = result.text or ""
        lower = text.lower()
        hits: list[str] = []

        for phrase in _FORBIDDEN_LITERALS:
            if phrase in lower:
                hits.append("em-dash" if phrase == "—" else phrase)

        for word in _FORBIDDEN_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", lower):
                hits.append(word)

        for transition in _FORBIDDEN_TRANSITIONS:
            pattern = rf"(?:^|[.!?]\s+){re.escape(transition)}\b"
            if re.search(pattern, text):
                hits.append(f"{transition} (sentence start)")

        ok = not hits
        detail = f"no voice violations: {ok}"
        if hits:
            detail += f" (found: {', '.join(hits)})"
        return ok, detail

    return check


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_eval(case: EvalCase) -> EvalResult:
    """Dispatch `case.agent` on `case.message` and run its checks."""
    log.info("eval %r: dispatching %s", case.name, case.agent)
    try:
        result = await dispatch(
            case.agent, case.message, dispatched_by="eval"
        )
    except Exception as exc:
        return EvalResult(
            case=case.name,
            passed=False,
            details=[f"dispatch raised: {type(exc).__name__}: {exc}"],
        )

    details: list[str] = []
    passed = True
    for check in case.checks:
        ok, detail = check(result)
        details.append(detail)
        passed = passed and ok
    return EvalResult(case=case.name, passed=passed, details=details)


async def run_suite(cases: list[EvalCase]) -> list[EvalResult]:
    """Run a list of EvalCases in order and return their results."""
    results: list[EvalResult] = []
    for case in cases:
        results.append(await run_eval(case))
    return results


__all__ = [
    "EvalCase",
    "EvalResult",
    "Check",
    "expect_contains",
    "expect_tool_used",
    "expect_no_tool_errors",
    "expect_no_voice_violations",
    "run_eval",
    "run_suite",
]
