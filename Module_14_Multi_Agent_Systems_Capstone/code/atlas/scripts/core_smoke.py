"""Browser smoke for the Today room (Neural Core), against a running Atlas.

  python -m scripts.core_smoke --base http://127.0.0.1:8010 [--out DIR] [--no-live]

Uses the Playwright Chromium the submitter uses (`python -m playwright install
chromium`). Checks:
  - / (live) and /?demo=1 load with zero console errors
  - demo: holding A for a second drops the headline count and shows the confirmed card
  - live, with /api/core/* answered in the browser: approve confirms with no Undo, a
    refused decline shows its reason, an empty list reads "All clear."
  - 390x844: no horizontal scroll, and every agent label sits inside the viewport
  - the frame cost at 1440x900 (three runs of 240 frames on the GPU), mean under 6 ms
Screenshots of demo at 1440x900 and 390x844 and of live at 1440x900 go to --out.

Read only: demo mode answers every /api call in the browser, and the live load
only reads. Nothing is approved or declined. Exit code 1 when a check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

GPU_ARGS = ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"]
FRAME_BUDGET_MS = 6.0
RENDERER = ("(() => { const c = document.createElement('canvas').getContext('webgl');"
            " const d = c && c.getExtension('WEBGL_debug_renderer_info');"
            " return d ? c.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'unknown'; })()")


def _page(browser: Any, base: str, path: str, w: int, h: int, errors: list[str]) -> Any:
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"{path} {w}x{h}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"{path} {w}x{h}: {e}"))
    page.goto(base + path, wait_until="domcontentloaded")
    page.wait_for_function("window.AtlasCore && document.body.dataset.room === 'bridge'", timeout=15000)
    page.wait_for_timeout(2500)
    return page


def _layout(page: Any) -> dict[str, Any]:
    return page.evaluate(
        "() => ({scrollW: Math.max(document.documentElement.scrollWidth, document.getElementById('nc').scrollWidth),"
        " clientW: document.getElementById('nc').clientWidth, innerW: innerWidth, innerH: innerHeight,"
        " labels: window.AtlasCore.labels()})")


def _mocked_approval(browser: Any, base: str, errors: list[str], fails: list[str]) -> dict[str, Any]:
    """The live card's approve and decline, with /api/core/* answered in the browser:
    nothing reaches the server's approval tables. Approve confirms; a refused
    decline shows its reason in the card and puts the hold back."""
    card = {"id": "11111111-1111-1111-1111-111111111111", "kind": "draft_email", "agent": "Client delivery",
            "title": "Approve an email draft", "preview": "The subject stays hidden.", "expires_at": None,
            "cost_estimate_usd": None}
    second = {**card, "id": "22222222-2222-2222-2222-222222222222"}
    state = {"demo": False, "roster": [{"id": "coo", "name": "COO"}, {"id": "cto", "name": "CTO"},
                                       {"id": "career", "name": "Career"},
                                       {"id": "client_delivery", "name": "Client delivery"}],
             "agents": [], "decisions": [card, second], "recent": [], "tz": "America/Chicago",
             "health": {"status": "normal"}, "overnight": {"done": 0, "spend": 0}}
    posts: list[dict[str, Any]] = []
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"mocked approval: {e}"))

    def on_state(route: Any) -> None:
        route.fulfill(json=state)

    def on_decide(route: Any) -> None:
        body = json.loads(route.request.post_data or "{}")
        posts.append(body)
        if body.get("decision") == "decline":
            route.fulfill(status=409, json={"detail": "This was already approved."})
            return
        state["decisions"] = [second]
        route.fulfill(json={"id": card["id"], "status": "approved", "repeat": False})

    page.route("**/api/core/state*", on_state)
    page.route("**/api/core/decisions/**", on_decide)
    page.route("**/api/core/events*", lambda r: r.abort())
    page.goto(base + "/?demo=0", wait_until="domcontentloaded")
    page.wait_for_function("window.AtlasCore && document.getElementById('nc-title').textContent.length > 0")
    page.wait_for_timeout(800)
    out: dict[str, Any] = {"before": page.inner_text("#nc-h1")}
    page.keyboard.down("a")
    page.wait_for_timeout(1000)
    page.keyboard.up("a")
    page.wait_for_timeout(300)
    out["optimistic"] = page.inner_text("#nc-sentb")
    page.wait_for_timeout(2600)
    out["confirmed"] = page.inner_text("#nc-sentb")
    out["undo_shown"] = page.is_visible("#nc-undo")
    page.wait_for_timeout(4200)
    out["after"] = page.inner_text("#nc-h1")
    page.click("#nc-decline")
    page.wait_for_timeout(600)
    out["decline_error"] = page.inner_text("#nc-err") if page.is_visible("#nc-err") else ""
    out["hold_back"] = page.is_visible("#nc-auth")
    state["decisions"] = []
    page.evaluate("window.AtlasCore.live.refresh()")
    page.wait_for_timeout(500)
    out["empty"] = [page.inner_text("#nc-h1"), page.inner_text("#nc-title")]
    out["posts"] = [{"decision": x.get("decision"), "has_key": bool(x.get("idempotency_key"))} for x in posts]
    ctx.close()
    ok = (out["before"].startswith("2 decisions") and out["optimistic"] == "Approving"
          and out["confirmed"] == "Approved" and not out["undo_shown"] and out["after"].startswith("1 decision")
          and out["decline_error"].startswith("Not declined.") and out["hold_back"]
          and out["empty"] == ["All clear.", "Nothing needs you"]
          and out["posts"] == [{"decision": "approve", "has_key": True}, {"decision": "decline", "has_key": True}])
    if not ok:
        fails.append(f"mocked live approval: {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://127.0.0.1:8010")
    p.add_argument("--out", type=Path, default=Path("logs") / "core-smoke")
    p.add_argument("--no-live", action="store_true", help="skip the live page (demo checks only)")
    args = p.parse_args(argv)
    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    fails: list[str] = []
    report: dict[str, Any] = {}
    with sync_playwright() as pw:
        # The GPU flags matter for the frame cost: headless Chromium otherwise draws the
        # canvas in software (SwiftShader), about ten times slower than the laptop's screen.
        browser = pw.chromium.launch(headless=True, args=GPU_ARGS)
        # demo, desktop: the hold
        page = _page(browser, args.base, "/?demo=1", 1440, 900, errors)
        page.screenshot(path=str(args.out / "demo-1440x900.png"))
        before = page.inner_text("#nc-h1")
        page.keyboard.down("a")
        page.wait_for_timeout(1000)
        page.keyboard.up("a")
        page.wait_for_timeout(300)
        after = page.inner_text("#nc-h1")
        confirmed = page.evaluate("document.getElementById('nc').classList.contains('authd')")
        report["hold"] = {"before": before, "after": after, "confirmed": confirmed}
        if not (before.startswith("2 decisions") and after.startswith("1 decision") and confirmed):
            fails.append(f"hold A: {before!r} -> {after!r}, confirmed={confirmed}")
        runs = [page.evaluate("window.AtlasCore.bench(240)") for _ in range(3)]
        report["frame_ms"] = {"mean": round(sum(r["mean"] for r in runs) / 3, 2),
                              "p95": max(r["p95"] for r in runs), "runs": [r["mean"] for r in runs],
                              "nodes": runs[0]["nodes"], "size": f"{runs[0]['w']}x{runs[0]['h']}"}
        report["renderer"] = page.evaluate(RENDERER)
        if report["frame_ms"]["mean"] > FRAME_BUDGET_MS:
            fails.append(f"frame cost {report['frame_ms']['mean']} ms > {FRAME_BUDGET_MS} ms on {report['renderer']}")
        page.context.close()
        # phone, demo and live
        for path in ["/?demo=1"] + ([] if args.no_live else ["/?demo=0"]):
            page = _page(browser, args.base, path, 390, 844, errors)
            lay = _layout(page)
            name = "demo" if "demo=1" in path else "live"
            if name == "demo":
                page.screenshot(path=str(args.out / "demo-390x844.png"))
            report[f"phone_{name}"] = {k: v for k, v in lay.items() if k != "labels"}
            if lay["scrollW"] > lay["clientW"]:
                fails.append(f"{name} 390x844: horizontal scroll ({lay['scrollW']} > {lay['clientW']})")
            out = [lb["name"] for lb in lay["labels"]
                   if lb["left"] < 0 or lb["right"] > lay["innerW"] or lb["top"] < 0]
            if out:
                fails.append(f"{name} 390x844: labels outside the viewport: {out}")
            page.context.close()
        report["live_approval"] = _mocked_approval(browser, args.base, errors, fails)
        if not args.no_live:
            page = _page(browser, args.base, "/?demo=0", 1440, 900, errors)
            page.screenshot(path=str(args.out / "live-1440x900.png"))
            report["live_headline"] = page.inner_text("#nc-h1")
            page.context.close()
        browser.close()
    report["console_errors"] = errors
    if errors:
        fails.append(f"{len(errors)} console error(s)")
    report["fails"] = fails
    print(json.dumps(report, indent=2))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
