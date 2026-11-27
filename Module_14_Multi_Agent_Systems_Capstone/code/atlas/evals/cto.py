"""CTO codebase-review eval

Each case dispatches the cto agent with a representative prompt
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
        name='cto_design_review',
        agent='cto',
        message='Review the design choice of running APScheduler in-process. Should Atlas move the scheduler to a separate worker, and what changes if we do?',
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
