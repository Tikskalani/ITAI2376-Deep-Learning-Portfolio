"""The real FormPage: Playwright in Atlas's own browser profile.

A dedicated persistent profile under data/browser/, never Trilok's
everyday Chrome, and no stealth plugins: Atlas does not hide that it is
automated from a site that checks. Playwright is an optional dependency
(`pip install -e .[submit]` then `python -m playwright install chromium`);
nothing else in Atlas imports this module unless the submitter runs.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

from core.config import REPO_ROOT
from core.jobs.submit.base import FormField

log = logging.getLogger(__name__)

PROFILE_DIR = REPO_ROOT / "data" / "browser" / "atlas"

# Where the submitter's Chromium lives: inside Atlas, not in AppData. A
# `playwright install` run from a packaged app's terminal (the Claude desktop
# app) lands in that app's private store, which the Windows service never sees
# (2026-10-22: "Executable doesn't exist" for a file plainly on disk).
BROWSERS_DIR = REPO_ROOT / "data" / "ms-playwright"


def _use_atlas_browsers() -> None:
    """Point Playwright at data/ms-playwright when it holds a browser, unless the
    environment already names a folder. Read by Playwright when it launches."""
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and BROWSERS_DIR.is_dir() and any(BROWSERS_DIR.iterdir()):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)


_use_atlas_browsers()

# Tags every control with data-atlas-ref and describes it by its label.
# Radio buttons are one field per group (their name), options = the labels.
_DISCOVER = r"""
() => {
  const text = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return text(l); }
    const wrap = el.closest('label'); if (wrap) return text(wrap);
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(id => text(document.getElementById(id))).join(' ').trim();
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
  };
  // A file input's own label is often its button ("Attach"); the question
  // ("Resume/CV") sits above it. Greenhouse names the input itself: id="resume".
  const generic = t => !t || /^(attach|upload|choose|browse|select|drop|add)\b/i.test(t);
  const fileLabel = el => {
    const own = labelOf(el);
    if (!generic(own)) return own;
    const named = (el.id || el.name || '').replace(/[_-]+/g, ' ').trim();
    if (/resume|cv|cover/i.test(named)) return named;
    // The nearest label above the input: before it in the page, never the next question's.
    for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++) {
      const above = [...p.querySelectorAll('legend, label, [class*="label"]')].filter(c =>
        !c.contains(el) && (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && !generic(text(c)));
      if (above.length) return text(above[above.length - 1]);
    }
    return named || own;
  };
  const visible = el => el.type === 'file' || !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [], groups = {};
  let i = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type) || el.disabled || !visible(el)) continue;
    // Ashby's "Autofill from resume" box: a file there rewrites the fields Atlas filled.
    if (type === 'file' && el.closest('[class*="autofill"]')) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio') {
      const name = el.name || ('radio-' + i);
      if (!groups[name]) {
        const fs = el.closest('fieldset');
        const legend = fs ? text(fs.querySelector('legend')) : '';
        groups[name] = {ref: 'radio:' + name, label: legend || labelOf(el), kind: 'radio', required, options: []};
        out.push(groups[name]);
      }
      groups[name].options.push(labelOf(el));
      groups[name].required = groups[name].required || required;
      continue;
    }
    const ref = 'atlas-' + (i++);
    el.setAttribute('data-atlas-ref', ref);
    let kind = 'text';
    if (el.tagName === 'TEXTAREA') kind = 'textarea';
    else if (el.tagName === 'SELECT') kind = 'select';
    else if (el.getAttribute('role') === 'combobox') kind = 'combobox';
    else if (['email', 'tel', 'url', 'number', 'checkbox', 'file'].includes(type)) kind = type;
    const options = kind === 'select' ? [...el.options].map(o => text(o)).filter(t => t && !/^select/i.test(t)) : [];
    const label = kind === 'file' ? fileLabel(el) : labelOf(el);
    out.push({ref, label, kind, required: required || /\*\s*$/.test(label), options});
  }
  return out;
}
"""

# Clicks the radio in a group whose label reads exactly `option`.
_CLICK_RADIO = r"""
([name, option]) => {
  for (const r of document.querySelectorAll(`input[type=radio][name="${CSS.escape(name)}"]`)) {
    const l = (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)) || r.closest('label');
    const t = ((l ? (l.innerText || l.textContent) : r.value) || '').replace(/\s+/g, ' ').trim();
    if (t === option) { r.click(); return true; }
  }
  return false;
}
"""

# A challenge the person must solve, not reCAPTCHA v3's invisible badge.
_CHALLENGE = r"""
() => [...document.querySelectorAll('iframe')].some(f => {
  const src = f.src || '';
  if (!/recaptcha|hcaptcha|turnstile|challenges\.cloudflare/.test(src)) return false;
  if (f.closest('.grecaptcha-badge')) return false;
  const r = f.getBoundingClientRect(), s = getComputedStyle(f);
  return r.width >= 100 && r.height >= 60 && s.visibility !== 'hidden' && s.display !== 'none';
})
"""


class BrowserPage:
    """FormPage over one Playwright page.

    The form can sit in an iframe: a company careers page that embeds its
    Greenhouse form (2026-10-23, Databricks: 22 fields in
    job-boards.greenhouse.io/embed/job_app, none on the page itself).
    fields() picks the frame holding the most fields and every later action
    goes to that frame."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._frame = page.main_frame

    @property
    def url(self) -> str:
        return str(self._page.url)

    def _el(self, ref: str) -> Any:
        return self._frame.locator(f'[data-atlas-ref="{ref}"]')

    async def open(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._page.wait_for_load_state("networkidle")

    async def fields(self) -> list[FormField]:
        best, found = self._page.main_frame, list[dict[str, Any]]()
        for frame in self._page.frames:
            try:
                got = await frame.evaluate(_DISCOVER)
            except Exception:  # noqa: BLE001 - a frame that navigated away or was detached holds no form
                continue
            if len(got) > len(found):
                best, found = frame, got
        self._frame = best
        return [FormField(**f) for f in found]

    async def fill(self, ref: str, value: str) -> None:
        await self._el(ref).fill(value)

    async def choose(self, ref: str, option: str) -> None:
        if ref.startswith("radio:"):
            clicked = await self._frame.evaluate(_CLICK_RADIO, [ref.split(":", 1)[1], option])
            if not clicked:
                raise RuntimeError(f"no radio button labelled {option!r}")
            return
        el = self._el(ref)
        if await el.evaluate("e => e.tagName") == "SELECT":
            await el.select_option(label=option)
            return
        # A typeahead: type the answer, then take the option that matches it.
        await el.click()
        await el.fill(option)
        match = self._frame.get_by_role("option", name=option, exact=False).first
        try:
            await match.click(timeout=3000)
        except Exception:  # noqa: BLE001 - no matching option listed: Enter takes the top one
            await el.press("Enter")

    async def tick(self, ref: str, on: bool = True) -> None:
        await self._el(ref).set_checked(on)

    async def upload(self, ref: str, path: Path) -> None:
        await self._el(ref).set_input_files(str(path))

    async def screenshot(self, path: Path) -> list[Path]:
        """The application form, in pages a screen high (core/jobs/submit/shots.py).
        The form only: an embedded form is its frame, a hosted one its <form>.
        Anything else, or a capture that fails, falls back to the whole page."""
        from core.jobs.submit import shots

        path.parent.mkdir(parents=True, exist_ok=True)
        target: Any = None
        try:
            if self._frame is not self._page.main_frame:
                target = await self._frame.frame_element()
            else:
                form = self._page.locator("form:has([data-atlas-ref])").first
                if await form.count():
                    target = form
            if target is not None:
                await target.screenshot(path=str(path))
        except Exception:  # noqa: BLE001 - a form that will not capture still gets the page
            log.warning("submitter: form screenshot failed; taking the whole page", exc_info=True)
            target = None
        if target is None:
            await self._page.screenshot(path=str(path), full_page=True)
        return shots.tile(path)

    async def press_submit(self) -> None:
        button = self._frame.get_by_role("button", name="Submit application", exact=False)
        if not await button.count():
            button = self._frame.locator("button[type=submit], input[type=submit]")
        await button.first.click()
        await self._page.wait_for_load_state("networkidle")

    async def text(self) -> str:
        """The page's text, plus the form frame's when the form is embedded
        (the thank-you note shows inside the frame)."""
        out = str(await self._page.inner_text("body"))
        if self._frame is not self._page.main_frame:
            try:
                out += "\n" + str(await self._frame.inner_text("body"))
            except Exception:  # noqa: BLE001 - the frame went away after the submit; the page text stands
                pass
        return out

    async def challenge_visible(self) -> bool:
        if await self._page.evaluate(_CHALLENGE):
            return True
        if self._frame is self._page.main_frame:
            return False
        try:
            return bool(await self._frame.evaluate(_CHALLENGE))
        except Exception:  # noqa: BLE001 - a detached frame shows no challenge
            return False


# What to run, from the repo root, in a terminal under the same Windows
# account as the AtlasWorker service (the browser installs per account).
INSTALL_COMMANDS = [
    r".venv\Scripts\python.exe -m pip install -e .[submit]",
    # Into data\ms-playwright, where the service can see it (see BROWSERS_DIR).
    r"$env:PLAYWRIGHT_BROWSERS_PATH = 'data\ms-playwright'; .venv\Scripts\python.exe -m playwright install chromium",
]


class Preflight(TypedDict):
    ok: bool
    detail: str
    commands: list[str]


_PREFLIGHT: dict[str, Any] = {"at": 0.0, "result": None}
_PREFLIGHT_TTL_OK = 3600.0
_PREFLIGHT_TTL_BAD = 600.0


async def _launch_once() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        await browser.close()


async def preflight(*, launch: Callable[[], Awaitable[None]] | None = None, fresh: bool = False) -> Preflight:
    """Can the submitter open its browser here? {"ok", "detail", "commands"}.

    Launches headless Chromium once and closes it (a second or two, no
    network), so a missing browser shows up before a job is claimed, not in
    the middle of one. Cached: an hour when fine, ten minutes when not."""
    cached: Preflight | None = _PREFLIGHT["result"]
    age = time.monotonic() - float(_PREFLIGHT["at"])
    if not fresh and cached is not None and age < (_PREFLIGHT_TTL_OK if cached["ok"] else _PREFLIGHT_TTL_BAD):
        return cached
    result: Preflight
    try:
        if launch is None:
            import playwright  # noqa: F401

        await (launch or _launch_once)()
        result = {"ok": True, "detail": "Chromium opens headless; the submitter is ready.", "commands": []}
    except ImportError:
        result = {"ok": False, "detail": "Playwright is not installed in Atlas's Python.", "commands": list(INSTALL_COMMANDS)}
    except Exception as exc:  # noqa: BLE001 - every failure means "not ready", said plainly
        text = str(exc)
        # The plain-words detail below hides the cause; the log keeps it, with the
        # environment the browser saw, so a failure only inside the Windows
        # service (2026-10-22) can be told apart from a missing install.
        log.warning("submitter preflight failed: %s: %s | LOCALAPPDATA=%s USERPROFILE=%s PLAYWRIGHT_BROWSERS_PATH=%s",
                    type(exc).__name__, text[:600], os.environ.get("LOCALAPPDATA"), os.environ.get("USERPROFILE"),
                    os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        if "Executable doesn't exist" in text or "playwright install" in text:
            detail = "Playwright is installed but its Chromium browser (the headless shell) is not, for this Windows account."
        else:
            detail = f"Chromium would not start: {text.splitlines()[0][:200] if text else type(exc).__name__}"
        result = {"ok": False, "detail": detail, "commands": list(INSTALL_COMMANDS)}
    _PREFLIGHT.update(at=time.monotonic(), result=result)
    return result


@asynccontextmanager
async def open_page(*, headless: bool = True) -> AsyncIterator[BrowserPage]:
    """One page in Atlas's persistent profile, closed afterwards."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e .[submit] && python -m playwright install chromium"
        ) from exc
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            yield BrowserPage(page)
        finally:
            await context.close()


__all__ = ["INSTALL_COMMANDS", "PROFILE_DIR", "Preflight", "BrowserPage", "open_page", "preflight"]
