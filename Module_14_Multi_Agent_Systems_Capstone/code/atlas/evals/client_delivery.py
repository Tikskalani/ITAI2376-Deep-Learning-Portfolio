"""Client Delivery agent eval

Each case dispatches the client_delivery agent with a representative prompt
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
        name='client_delivery_status_update',
        agent='client_delivery',
        message='Draft a status update for a client engagement that is halfway through. We are on track, but a scope-creep request came in to add Slack alerts when the original scope was email only.',
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
