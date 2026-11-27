"""CFO money-domain eval

Each case dispatches the cfo agent with a representative prompt
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
        name='cfo_cash_flow_query',
        agent='cfo',
        message="What's my cash flow look like this month?",
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
