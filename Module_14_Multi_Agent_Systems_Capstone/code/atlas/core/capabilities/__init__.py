"""Capabilities (K2C): a shared video, article or page becomes a source-checked,
versioned procedure Atlas agents can retrieve and apply. Trilok's spec of
2026-10-20; docs/capabilities.md; migration 038.

    capture      -> capture.py    the source, deduplicated; the video and its description
    extraction   -> extract.py    timed transcript, targeted frames read for screen text
    resources    -> build.py      one referenced resource resolved through the guarded fetch
    claims       -> build.py      actionable claims with the evidence they cite
    checking     -> build.py      at least one consequential claim against a primary source
    procedure    -> build.py      prerequisites, steps citing evidence, limitations, missing
    approval     -> store.py      immutable versions; approval and withdrawal are records
    retrieval    -> retrieval.py  the CTO's search, get and outcome tools
    Claude Code  -> claude_skill.py  an approved version installed as a skill

pipeline.py runs the stages with visible states and the per-source budget.
Nothing here executes downloaded content; procedures are reference data,
never instructions.
"""
