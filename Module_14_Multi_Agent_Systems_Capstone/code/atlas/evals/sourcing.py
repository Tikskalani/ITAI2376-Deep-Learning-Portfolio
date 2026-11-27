"""Sourcing supplier-evaluation eval

Each case dispatches the sourcing agent with a representative prompt
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
        name='sourcing_alibaba_score',
        agent='sourcing',
        message='Evaluate this Alibaba listing. Supplier: Foshan Mei Yuan Furniture. Quoted: $58/unit at MOQ 100. Lead time 75 days. Verified Supplier badge, no Trade Assurance. CARB Phase 2 mentioned in description, no certificate uploaded. 110V outlets confirmed.',
        checks=[expect_no_voice_violations(), expect_no_tool_errors(), expect_contains("score")],
    ),
]
