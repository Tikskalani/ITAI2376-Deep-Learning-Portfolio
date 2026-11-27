"""Personal Ops household eval

Each case dispatches the personal_ops agent with a representative prompt
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
        name='personal_ops_monthly_view',
        agent='personal_ops',
        message='What is coming up household-wise this month?',
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
