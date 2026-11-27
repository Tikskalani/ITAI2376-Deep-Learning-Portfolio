"""Property Ops drafting eval (regression check)

Each case dispatches the property_ops agent with a representative prompt
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
        name='property_ops_eviction_settlement',
        agent='property_ops',
        message='Draft an eviction settlement offer for a tenant who owes $2,400 in back rent and wants to vacate. Propose waiving the balance in exchange for keys returned by next Friday and a mutual release.',
        checks=[expect_no_voice_violations(), expect_no_tool_errors(), expect_contains("2,400"), expect_contains("keys")],
    ),
]
