"""What every board adapter works against: a form page, its fields, and
reading the page for the things that stop Atlas.

The submitter never touches Playwright directly. It drives a FormPage, so
the adapters and the whole submit path run in tests against a fake page,
and core/jobs/submit/browser.py is the only file that knows a browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

KINDS = ("text", "textarea", "email", "tel", "url", "number", "select", "combobox", "radio", "checkbox", "file")


@dataclass
class FormField:
    ref: str            # the driver's handle for the control
    label: str          # the visible question, as the applicant reads it
    kind: str = "text"  # one of KINDS
    required: bool = False
    options: list[str] = field(default_factory=list)


class FormPage(Protocol):
    """One application form in Atlas's own browser profile."""

    @property
    def url(self) -> str: ...

    async def open(self, url: str) -> None: ...

    async def fields(self) -> list[FormField]: ...

    async def fill(self, ref: str, value: str) -> None: ...

    async def choose(self, ref: str, option: str) -> None: ...

    async def tick(self, ref: str, on: bool = True) -> None: ...

    async def upload(self, ref: str, path: Path) -> None: ...

    async def screenshot(self, path: Path) -> list[Path] | None: ...

    async def press_submit(self) -> None: ...

    async def text(self) -> str: ...

    async def challenge_visible(self) -> bool:
        """A CAPTCHA the person must solve is on screen (not an invisible badge)."""
        ...


_CAPTCHA = ("captcha", "verify you are human", "i'm not a robot", "i am not a robot", "are you a robot")
_LOGIN = ("sign in to apply", "log in to apply", "login to apply", "create an account to apply",
          "sign in to continue your application")
_CODE = ("security code", "verification code", "enter the code we sent", "enter the 8-character code")
_CONFIRM = (
    "thank you for applying", "thanks for applying", "application has been submitted",
    "application was submitted", "application has been received", "we have received your application",
    "we've received your application", "your application was successfully submitted",
    "application submitted",
)


def _has(text: str, phrases: tuple[str, ...]) -> str | None:
    low = " ".join((text or "").lower().split())
    return next((p for p in phrases if p in low), None)


async def obstacle(page: FormPage) -> str | None:
    """Why Atlas must hand this form to Trilok, or None. Never bypassed."""
    if await page.challenge_visible():
        return "a CAPTCHA is on the page"
    text = await page.text()
    if (hit := _has(text, _LOGIN)):
        return f"the board wants a login ({hit!r})"
    if (hit := _has(text, _CODE)):
        return f"the board emailed a code to type in ({hit!r})"
    if (hit := _has(text, _CAPTCHA)):
        return f"the page asks for a human check ({hit!r})"
    return None


async def confirmed(page: FormPage) -> bool:
    """The page says the application went in."""
    if re.search(r"/(confirmation|thank[-_]?you|submitted)\b", page.url or "", re.I):
        return True
    return _has(await page.text(), _CONFIRM) is not None


def pick_option(answer: str, options: list[str]) -> str | None:
    """The option that carries `answer`: exact, then prefix, then containment.
    None when nothing matches; the caller pauses instead of guessing."""
    a = " ".join(answer.lower().split())
    if not a:
        return None
    norm = {o: " ".join(o.lower().split()) for o in options}
    for o, n in norm.items():
        if n == a:
            return o
    # Whole words only: "No" must not pick "Not specified".
    for o, n in norm.items():
        if re.match(rf"{re.escape(a)}\b", n) or (len(n) >= 2 and re.match(rf"{re.escape(n)}\b", a)):
            return o
    hits = [o for o, n in norm.items() if re.search(rf"\b{re.escape(a)}\b", n)]
    return hits[0] if len(hits) == 1 else None


__all__ = ["KINDS", "FormField", "FormPage", "confirmed", "obstacle", "pick_option"]
