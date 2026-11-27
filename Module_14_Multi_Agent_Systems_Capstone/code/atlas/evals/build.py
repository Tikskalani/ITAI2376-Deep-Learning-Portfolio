"""Build agent productize eval

Each case dispatches the build agent with a representative prompt
and checks voice plus no tool errors. Running these spends real Claude
API tokens; do it deliberately, not on every commit.
"""

from __future__ import annotations

from core.runtime.evals import (
    EvalCase,
    expect_contains,
    expect_no_tool_errors,
    expect_no_voice_violations,
)

CASES: list[EvalCase] = [
    EvalCase(
        name='build_dashboard_spec',
        agent='build',
        message='Draft a Phase-1 spec for a small internal dashboard that shows weekly Example Property Group rent collection across the six communities at a glance.',
        checks=[expect_no_voice_violations(), expect_no_tool_errors(), expect_contains("problem"), expect_contains("MVP")],
    ),
]
