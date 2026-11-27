"""Opportunity Scout triage eval

Each case dispatches the opportunity_scout agent with a representative prompt
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
        name='opportunity_scout_low_budget_gig',
        agent='opportunity_scout',
        message="Forwarded Upwork posting. 'Need experienced AI engineer to build chatbot for our Shopify store, $200 budget, want it done by end of week.' Worth my time?",
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
