"""The dashboard is a static shell plus partials and scripts, with no build
step. These checks keep the pieces in step: every partial the boot loader
names exists, the service worker caches only files that exist, and the
shell loads the fetch guard before anything can write."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "core" / "web" / "static"


def _partials() -> list[str]:
    boot = (STATIC / "atlas" / "boot.js").read_text(encoding="utf-8")
    m = re.search(r"ATLAS_PARTIALS\s*=\s*\[([^\]]*)\]", boot)
    assert m, "boot.js must declare window.ATLAS_PARTIALS"
    return re.findall(r'"([a-z]+)"', m.group(1))


def test_every_partial_exists():
    names = _partials()
    assert set(names) == {"bridge", "comms", "plan", "career", "staff", "inbox", "memory", "settings", "overlays"}
    for name in names:
        html = (STATIC / "rooms" / f"{name}.html").read_text(encoding="utf-8")
        assert f'data-room="{name}"' in html, name


def test_service_worker_caches_real_files():
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    urls = re.findall(r'"(/static/[^"]+)"', sw)
    assert urls, "the shell cache lists the static files"
    for url in urls:
        assert (STATIC / url.removeprefix("/static/")).is_file(), url
    for name in _partials():
        assert f"/static/rooms/{name}.html" in sw, name


def _own_files() -> list[Path]:
    return [p for p in STATIC.rglob("*") if p.is_file() and "vendor" not in p.parts
            and p.suffix in {".html", ".css", ".js"}]


def test_no_v2_look():
    for p in _own_files():
        text = p.read_text(encoding="utf-8")
        for old in ("Geist", "#5eead4", "#08090b", "#2dd4bf"):
            assert old not in text, f"{p.relative_to(STATIC)} still carries the v2 look ({old})"


def test_tokens_match_spec():
    tokens = (STATIC / "atlas" / "tokens.css").read_text(encoding="utf-8")
    # The Neural Core palette (2026-10-22): every room shares the Today room's colors.
    for name, value in {"--bg": "#04060A", "--h": "#7FB2FF", "--h2": "#FFB020", "--bad": "#FF5A6A",
                        "--t1": "#EAF1FF", "--t2": "#C0CCE4", "--t3": "#8A98B4"}.items():
        assert re.search(rf"{re.escape(name)}:\s*{value};", tokens), name


async def test_shell_files_are_revalidated():
    import httpx

    from core.web.api import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        for url in ("/", "/static/atlas/hud.css", "/static/rooms/bridge.html"):
            r = await c.get(url)
            assert r.status_code == 200, url
            assert r.headers.get("cache-control") == "no-cache", url
        r = await c.get("/static/icons/icon-192.png")
        assert r.headers.get("cache-control") != "no-cache"


# The eight rooms (audit 2026-10-22, finding 09), each sub-tab in tab order.
ROOM_SUBS = {
    "comms": ["channel", "briefings", "recordings"],
    "plan": ["week", "night", "routines"],
    "career": ["queue", "pipeline", "discover", "profile"],
    "staff": ["agents", "runs", "playbook"],
    "inbox": ["approvals", "skills", "signals", "notifications"],
    "memory": ["facts", "people", "projects", "map"],
    "settings": ["connections", "trust", "ledger"],
}


def test_rooms_in_partials():
    for room, subs in ROOM_SUBS.items():
        html = (STATIC / "rooms" / f"{room}.html").read_text(encoding="utf-8")
        assert f"x-show=\"room === '{room}'\"" in html, room
        tabs = re.search(r'role="tablist".*?</div>', html, flags=re.S)
        assert tabs, room
        found = re.findall(rf"goRoom\('{room}', '([a-z]+)'\)", tabs.group(0))
        assert found == subs, (room, found)
        for sub in subs:
            assert f"x-show=\"sub.{room} === '{sub}'\"" in html, (room, sub)
    html = (STATIC / "rooms" / "bridge.html").read_text(encoding="utf-8")
    assert "x-show=\"room === 'bridge'\"" in html


def test_app_knows_the_same_rooms():
    app = (STATIC / "atlas" / "app.js").read_text(encoding="utf-8")
    for room, subs in ROOM_SUBS.items():
        assert f'{room}: [{", ".join(chr(34) + s + chr(34) for s in subs)}]' in app, room


def test_every_old_address_still_lands_somewhere():
    """Pushes, bookmarks and old mail carry the six-room hashes; each one
    must map to a room and sub-tab that exist now (finding 08)."""
    app = (STATIC / "atlas" / "app.js").read_text(encoding="utf-8")
    moved = dict(re.findall(r'"([a-z]+/[a-z]*)": \["([a-z]+)", "[a-z]+"\]', app))
    targets = re.findall(r'"[a-z]+/[a-z]*": \["([a-z]+)", "([a-z]+)"\]', app)
    for old in ("missions/jobs", "missions/plan", "memory/about", "memory/skills", "memory/ledger", "inbox/trust",
                "links/", "connections/"):
        assert old in moved, old
    for room, sub in targets:
        assert sub in ROOM_SUBS.get(room, []), (room, sub)


def test_four_text_sizes_and_three_breakpoints():
    """Findings 20 and 21: text under 20px uses the four --fs tokens, and
    every width query is one of sm 640, md 900, lg 1180."""
    files = [*sorted((STATIC / "atlas").glob("*.css")), *sorted((STATIC / "rooms").glob("*.html")), STATIC / "index.html"]
    for p in files:
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:font-size:\s*|font:\s*(?:italic\s+)?[0-9]{3}\s+)([0-9.]+)px", text):
            assert float(m.group(1)) >= 20, f"{p.name}: {m.group(0)} (use var(--fs-1..4))"
        for m in re.finditer(r"\((?:max|min)-width:\s*([0-9]+)px\)", text):
            assert int(m.group(1)) in (640, 900, 1180), f"{p.name}: {m.group(0)}"
    tokens = (STATIC / "atlas" / "tokens.css").read_text(encoding="utf-8")
    for name, value in {"--fs-1": "11px", "--fs-2": "12.5px", "--fs-3": "14px", "--fs-4": "18px"}.items():
        assert f"{name}: {value};" in tokens, name


def test_the_overlays_are_not_a_room():
    """2026-10-24: overlays.html was wrapped in class="room", whose entrance
    animation leaves a transform; a transformed ancestor makes position:
    fixed relative to it, so every sheet (traces, approvals, briefs) was
    centred on a 48 px strip and ran off the top of the screen."""
    html = (STATIC / "rooms" / "overlays.html").read_text(encoding="utf-8")
    first = html.split("\n", 1)[0]
    assert 'data-room="overlays"' in first and 'class="room"' not in first


def test_no_landmark_per_room():
    """Finding 19: one main, one header and the room nav; sub-tabs are tablists, not navs."""
    for p in sorted((STATIC / "rooms").glob("*.html")):
        html = p.read_text(encoding="utf-8")
        assert "<nav" not in html and "<header" not in html, p.name


def test_no_alpine_template_inside_svg():
    # Alpine cannot clone a <template> in the SVG namespace; it throws and
    # the element never renders. SVG that repeats is rendered by dial.js.
    for p in [STATIC / "index.html", *sorted((STATIC / "rooms").glob("*.html"))]:
        html = p.read_text(encoding="utf-8")
        for block in re.findall(r"<svg\b.*?</svg>", html, flags=re.S):
            assert "<template" not in block, f"{p.name}: <template> inside <svg>"


def test_demo_mode_is_invented():
    # Demo mode is shown to strangers: nothing from his real life may appear in
    # it, and it must say on screen that it is not real.
    demo = (STATIC / "atlas" / "demo.js").read_text(encoding="utf-8")
    for real in ("Lee", "Kirkdale", "Example Property Group", "Sidework", "Chris", "Jordan", "Databricks",
                 "atlas-dev", "Hometown", "HCC", "Houston Community College", "a tutoring platform"):
        assert real.lower() not in demo.lower(), f"demo.js mentions {real}"
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "DEMO DATA" in index and index.index("atlas/demo.js") < index.index("atlas/app.js")


def test_index_is_a_shell():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in _partials():
        assert f'data-slot="{name}"' in html, name
    assert html.index("atlas/fetch-guard.js") < html.index("atlas/boot.js")
    assert "alpine-3.13.5" not in html, "boot.js loads Alpine after the partials"
