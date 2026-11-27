"""Board adapters for the job submitter (core/jobs/submitter.py).

One per applicant tracking system: Greenhouse (PR D), then Lever, Ashby and
Workable (core/jobs/submit/boards.py, 2026-10-22), which share Greenhouse's
question-by-question fill. A posting on a board with no adapter is handed to
Trilok as "needs your click" with everything ready.
"""

from __future__ import annotations

from typing import Any

from core.jobs.submit import boards, greenhouse

ADAPTERS: dict[str, Any] = {
    greenhouse.ATS: greenhouse,
    boards.lever.ATS: boards.lever,
    boards.ashby.ATS: boards.ashby,
    boards.workable.ATS: boards.workable,
}

__all__ = ["ADAPTERS"]
