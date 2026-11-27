"""Lever, Ashby and Workable application forms.

Greenhouse taught the approach (core/jobs/submit/greenhouse.py): fields are
found by the question the applicant reads, contact fields come from the
standard answers, and every other question goes through core.jobs.answers.
A required question with no confident answer is never guessed; it comes back
as missing and is asked once. That fill is board-neutral, so these three
boards reuse it; what differs is where each board's form lives.

Each board's posting page links to its form at a fixed path:

  Lever     https://jobs.lever.co/<board>/<id>          ->  .../apply
  Ashby     https://jobs.ashbyhq.com/<board>/<id>       ->  .../application
  Workable  https://apply.workable.com/<board>/j/<code> ->  .../apply/

Until Trilok turns dry run off (config/jobs.yaml), every form is filled and
screenshotted, never sent; the screenshots in Jobs are how a board's form is
checked before the first real submit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from core.jobs.submit.greenhouse import Files, Filled, fill


def _base(posting: dict[str, Any], host: str, fallback: str) -> str:
    """The posting's own page on the board, or one built from board and id."""
    url = str(posting.get("url") or "").split("?", 1)[0].split("#", 1)[0]
    if host not in (urlparse(url).hostname or ""):
        url = fallback.format(board=posting.get("board"), id=posting.get("external_id"))
    return url.rstrip("/")


def lever_url(posting: dict[str, Any]) -> str:
    base = _base(posting, "lever.co", "https://jobs.lever.co/{board}/{id}")
    return base if base.endswith("/apply") else base + "/apply"


def ashby_url(posting: dict[str, Any]) -> str:
    base = _base(posting, "ashbyhq.com", "https://jobs.ashbyhq.com/{board}/{id}")
    return base if base.endswith("/application") else base + "/application"


def workable_url(posting: dict[str, Any]) -> str:
    base = _base(posting, "workable.com", "https://apply.workable.com/{board}/j/{id}")
    return (base if base.endswith("/apply") else base + "/apply") + "/"


def _adapter(ats: str, url: Any) -> SimpleNamespace:
    """The adapter shape the submitter calls: ATS, application_url, fill."""
    return SimpleNamespace(ATS=ats, application_url=url, fill=fill, Files=Files, Filled=Filled)


lever = _adapter("lever", lever_url)
ashby = _adapter("ashby", ashby_url)
workable = _adapter("workable", workable_url)

__all__ = ["ashby", "ashby_url", "lever", "lever_url", "workable", "workable_url"]
