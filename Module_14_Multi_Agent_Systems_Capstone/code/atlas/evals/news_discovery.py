"""News & Discovery triage eval

Each case dispatches the news_discovery agent with a representative prompt
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
        name='news_discovery_wikipedia_rag',
        agent='news_discovery',
        message='Read this and tell me if it matters: https://en.wikipedia.org/wiki/Retrieval-augmented_generation',
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
