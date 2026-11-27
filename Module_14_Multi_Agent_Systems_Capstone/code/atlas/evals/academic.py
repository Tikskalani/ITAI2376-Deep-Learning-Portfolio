"""Academic agent voice eval cases.

Each case dispatches the Academic agent with a real assignment-shaped
prompt and checks the reply against the voice guide's strict bans (no
em-dash, no AI tells, no forbidden words, no banned transition starters)
and that no tool calls errored.

Running these spends real Claude API tokens. Do it deliberately, not on
every commit.
"""

from __future__ import annotations

from core.runtime.evals import (
    EvalCase,
    expect_no_tool_errors,
    expect_no_voice_violations,
)

CASES: list[EvalCase] = [
    EvalCase(
        name="academic_discussion_post_cache",
        agent="academic",
        message=(
            "Draft a 200-word reply for an HCC CS class discussion: "
            "'Why is cache invalidation called one of the two hard "
            "problems in CS?' Write as Trilok."
        ),
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
    EvalCase(
        name="academic_short_response_sql_nosql",
        agent="academic",
        message=(
            "Draft a 250-word response for an HCC CS/IT assignment on the "
            "tradeoffs between SQL and NoSQL databases. Write as Trilok."
        ),
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
    EvalCase(
        name="academic_study_guide_big_o",
        agent="academic",
        message=(
            "Prepare a short study guide on big-O notation for an HCC "
            "intro algorithms exam: definitions, the common complexity "
            "classes with examples, and the two or three pitfalls "
            "students miss most."
        ),
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
    EvalCase(
        name="academic_intro_self",
        agent="academic",
        message=(
            "Draft a 150-word introduction post for a CS/IT class: who I "
            "am, what I do, what I want from the course. Write as Trilok."
        ),
        checks=[expect_no_voice_violations(), expect_no_tool_errors()],
    ),
]
