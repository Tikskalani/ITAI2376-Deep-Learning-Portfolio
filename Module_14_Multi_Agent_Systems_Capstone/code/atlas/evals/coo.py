"""COO deadline-tracking eval

Each case dispatches the coo agent with a representative prompt
and checks voice plus no tool errors. Running these spends real Claude
API tokens; do it deliberately, not on every commit.
"""

from __future__ import annotations

from core.runtime.evals import (
    EvalCase,
    expect_no_tool_errors,
    expect_no_voice_violations,
)

CASES: list[EvalCase] = [
    EvalCase(
        name='coo_slipping_check',
        agent='coo',
        message="What's slipping in the next 30 days?",
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
