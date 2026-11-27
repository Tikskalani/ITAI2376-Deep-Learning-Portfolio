"""CEO synthesis eval

Each case dispatches the ceo agent with a representative prompt
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
        name='ceo_recall_self',
        agent='ceo',
        message='Tell me what Atlas knows about Side Shop.',
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
