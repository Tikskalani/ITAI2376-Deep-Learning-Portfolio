"""Greenhouse application forms.

Fields are found by the question the applicant reads, not by brittle
selectors, so a board that reorders or restyles its form still fills.
Contact fields come from the standard answers; everything else goes
through core.jobs.answers, which knows the standard wording and every
question Trilok has answered once. A required question with no confident
answer is never guessed: it is returned as missing and asked once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.jobs import answers
from core.jobs.submit.base import FormField, FormPage, pick_option

ATS = "greenhouse"


@dataclass
class Files:
    resume: Path
    cover: Path
    cover_text: str


@dataclass
class Filled:
    answers: dict[str, str] = field(default_factory=dict)  # question -> what went in
    missing: list[str] = field(default_factory=list)       # required, no confident answer
    uploads: int = 0                                         # file fields on the form
    resume_placed: bool = False                              # the resume went into one of them


def application_url(posting: dict[str, Any]) -> str:
    """The posting's own Greenhouse page, or the board's hosted one."""
    url = posting.get("url") or ""
    if "greenhouse.io" in (urlparse(url).hostname or ""):
        return url
    return f"https://job-boards.greenhouse.io/{posting['board']}/jobs/{posting['external_id']}"


def _key(label: str) -> str:
    """The label without required marks or punctuation, lowercased."""
    text = re.sub(r"\s*(\*|\(required\)|\(optional\))\s*", " ", label or "", flags=re.I)
    return " ".join(re.sub(r"[^a-z0-9/ ]+", " ", text.lower()).split())


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    if len(parts) < 2:
        return full, ""
    return " ".join(parts[:-1]), parts[-1]


async def _answer(label: str, profile: dict[str, str], files: Files) -> str | None:
    """What goes in this field, or None if Atlas has no confident answer."""
    k = _key(label)
    first, last = _split_name(profile.get("full_name", ""))
    direct = {
        "first name": first, "legal first name": first, "preferred first name": first, "preferred name": first,
        "last name": last, "legal last name": last, "surname": last, "family name": last,
        "full name": profile.get("full_name"), "name": profile.get("full_name"),
        "email": profile.get("email"), "email address": profile.get("email"),
        "phone": profile.get("phone"), "phone number": profile.get("phone"),
        "mobile phone": profile.get("phone"),
        "location": profile.get("location"), "location city": profile.get("location"),
        "city": profile.get("location"), "current location": profile.get("location"),
        "linkedin": profile.get("linkedin_url"), "linkedin profile": profile.get("linkedin_url"),
        "linkedin url": profile.get("linkedin_url"),
        "github": profile.get("github_url"), "github url": profile.get("github_url"),
        "website": profile.get("portfolio_url") or profile.get("github_url"),
        "portfolio": profile.get("portfolio_url") or profile.get("github_url"),
        "cover letter": files.cover_text,
    }
    if k in direct:
        return direct[k] or None
    return await answers.answer_for(label)


async def fill(page: FormPage, posting: dict[str, Any], files: Files) -> Filled:
    """Fill every field Atlas can answer. Does not submit."""
    profile = await answers.form_profile()
    out = Filled()
    for f in await page.fields():
        k = _key(f.label)
        if not k:
            continue
        if f.kind == "file":
            out.uploads += 1
            if "cover" in k:
                await page.upload(f.ref, files.cover)
                out.answers[f.label] = files.cover.name
            elif any(w in k for w in ("resume", "cv")):
                await page.upload(f.ref, files.resume)
                out.answers[f.label] = files.resume.name
                out.resume_placed = True
            elif f.required:
                out.missing.append(f.label)
            continue
        value = await _answer(f.label, profile, files)
        if value is None:
            if f.required:
                out.missing.append(f.label)
            continue
        if f.kind in ("select", "radio"):
            option = pick_option(value, f.options)
            if option is None:
                if f.required:
                    out.missing.append(f.label)
                continue
            await page.choose(f.ref, option)
            out.answers[f.label] = option
        elif f.kind == "combobox":
            # Options load as you type; the driver picks the one that matches.
            await page.choose(f.ref, value)
            out.answers[f.label] = value
        elif f.kind == "checkbox":
            on = value.strip().lower() in ("yes", "true", "i agree", "agree", "y")
            await page.tick(f.ref, on)
            out.answers[f.label] = "yes" if on else "no"
        else:
            await page.fill(f.ref, value)
            out.answers[f.label] = value
    return out


__all__ = ["ATS", "Files", "Filled", "FormField", "application_url", "fill"]
