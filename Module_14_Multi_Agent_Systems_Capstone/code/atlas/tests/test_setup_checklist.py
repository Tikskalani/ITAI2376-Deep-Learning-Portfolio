"""The setup checklist, the submitter's preflight, push and the Zapier 404 (r12).

No provider is called, no browser is launched, no model runs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core import setup_checklist
from core.jobs import submitter
from core.jobs.submit import browser
from tests import livedb

# The two tasks the CEO put on the Plan on 2026-10-11, word for word.
REAL_CHORES = [
    ("Run scripts/connect.bat to connect Google, Canvas and GitHub", "google"),
    ("Install the submitter browser (pip install -e .[submit], then playwright install chromium)", "submitter"),
]


@pytest.mark.parametrize("title,item", REAL_CHORES + [
    ("Subscribe the phone to push notifications", "push"),
    ("Connect Canvas so due dates land", "canvas"),
    ("Connect GitHub with a read-only token", "github"),
])
def test_chores_are_recognised(title, item):
    assert setup_checklist.chore_for(title) == item


def test_the_submitter_uses_atlas_own_browser_folder(tmp_path, monkeypatch):
    """A browser installed from the Claude app's terminal lands in that app's
    private store, invisible to the Windows service; data/ms-playwright is not."""
    folder = tmp_path / "ms-playwright"
    monkeypatch.setattr(browser, "BROWSERS_DIR", folder)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    browser._use_atlas_browsers()
    assert "PLAYWRIGHT_BROWSERS_PATH" not in __import__("os").environ, "no folder yet: Playwright's default"
    (folder / "chromium_headless_shell-1243").mkdir(parents=True)
    browser._use_atlas_browsers()
    assert __import__("os").environ["PLAYWRIGHT_BROWSERS_PATH"] == str(folder)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", r"D:\elsewhere")
    browser._use_atlas_browsers()
    assert __import__("os").environ["PLAYWRIGHT_BROWSERS_PATH"] == r"D:\elsewhere", "an explicit folder wins"
    assert any("data\\ms-playwright" in c for c in browser.INSTALL_COMMANDS)


@pytest.mark.parametrize("title", ["Email the lender by Friday 3pm", "Renew auto insurance", "Push the HCC essay draft"])
def test_real_tasks_are_not_chores(title):
    assert setup_checklist.chore_for(title) is None


def _status(google_ok=None, canvas_ok=None, github_ok=None):
    def blk(ok):
        return {"ok": ok, "detail": "connected" if ok else "not connected"}
    return {"google": {**blk(google_ok), "needs_reconnect": False}, "canvas": blk(canvas_ok),
            "github": blk(github_ok), "alarm": blk(None), "mcp": {"servers": []}}


def _ready(ok: bool):
    return {"ok": ok, "detail": "ready" if ok else "Chromium missing",
            "commands": [] if ok else list(browser.INSTALL_COMMANDS)}


async def test_checklist_reads_live_state_and_push_turns_green_when_a_device_subscribes(monkeypatch):
    from core import db

    async with livedb.shadow(monkeypatch, "push_subscriptions"):
        with (
            patch("core.connect.status", new=AsyncMock(return_value=_status(google_ok=True))),
            patch("core.jobs.submit.browser.preflight", new=AsyncMock(return_value=_ready(False))),
            patch("core.surfaces.notify.push_configured", return_value=True),
        ):
            first = await setup_checklist.checklist()
            items = {i["id"]: i for i in first["items"]}
            assert [i["id"] for i in first["items"]] == ["google", "canvas", "github", "alarm", "push", "submitter"]
            assert items["google"]["done"] and not items["canvas"]["done"]
            assert items["alarm"]["label"] == "Set the outside alarm" and not items["alarm"]["done"]
            assert items["alarm"]["action"] == {"kind": "tile", "tile": "alarm"}
            assert not items["push"]["done"] and "Subscribe this device" in " ".join(items["push"]["steps"])
            assert items["submitter"]["commands"] == browser.INSTALL_COMMANDS
            assert first["done"] == 1 and not first["complete"]

            await db.execute(
                "insert into push_subscriptions (endpoint, p256dh, auth) values ('https://push.example/1', 'k', 'a')"
            )
            items = {i["id"]: i for i in (await setup_checklist.checklist())["items"]}
            assert items["push"]["done"] and items["push"]["steps"] == []
            assert items["push"]["devices"] == 1


# ------------------------------------------------------------------ the Plan stays clean


@pytest.mark.parametrize("title,item", REAL_CHORES)
async def test_the_ceo_cannot_put_a_setup_chore_on_the_plan(title, item):
    from agents.ceo.tools import _add_task

    with patch("core.planner.quick_add", new=AsyncMock()) as add:
        out = await _add_task({"text": title})
    add.assert_not_awaited()
    assert out["added"] is False and out["setup_item"] == item and "Settings, Connections" in out["note"]


async def test_an_agent_saying_the_same_task_twice_gets_the_open_one_back(monkeypatch):
    from core import planner

    async with livedb.shadow(monkeypatch, "deadlines", "triggers"):
        first = await planner.quick_add("Draft the Kirkdale proposal outline", source="ceo")
        again = await planner.quick_add("draft the kirkdale proposal outline", source="ceo")
        assert first["created"] is True and again["created"] is False and again["id"] == first["id"]
        # The owner can still write the same words twice on purpose.
        mine = await planner.quick_add("Draft the Kirkdale proposal outline", source="owner")
        assert mine["created"] is True and mine["id"] != first["id"]


# ------------------------------------------------------------------ submitter preflight


async def test_preflight_names_the_missing_browser_and_the_command():
    async def missing():
        raise RuntimeError("BrowserType.launch: Executable doesn't exist at C:\\...\\chrome-headless-shell.exe")

    out = await browser.preflight(launch=missing, fresh=True)
    assert out["ok"] is False and "headless shell" in str(out["detail"])
    assert out["commands"] == browser.INSTALL_COMMANDS

    async def works():
        return None

    assert (await browser.preflight(launch=works, fresh=True))["ok"] is True
    # Cached: a later call does not launch again.
    boom = AsyncMock(side_effect=AssertionError("launched twice"))
    assert (await browser.preflight(launch=boom))["ok"] is True


async def test_the_submitter_claims_nothing_while_the_browser_is_missing(monkeypatch):
    monkeypatch.setattr(submitter, "_BROWSER_WARNED", set())
    note = AsyncMock()
    with (
        patch("core.jobs.submitter.due", new=AsyncMock(return_value=["q1", "q2"])),
        patch("core.jobs.submitter.submit_one", new=AsyncMock()) as one,
        patch("core.jobs.submit.browser.preflight", new=AsyncMock(return_value=_ready(False))),
        patch("core.surfaces.notify.notify", new=note),
    ):
        assert await submitter.run(cfg={"dry_run": True, "per_run": 5}) == {"waiting_browser": 2}
        assert await submitter.run(cfg={"dry_run": True, "per_run": 5}) == {"waiting_browser": 2}
    one.assert_not_awaited()
    note.assert_awaited_once()  # told once a day, not every twenty minutes
    body = note.await_args.args[2]
    assert "playwright install chromium" in body


async def test_the_submitter_runs_when_the_browser_is_ready():
    with (
        patch("core.jobs.submitter.due", new=AsyncMock(return_value=["q1"])),
        patch("core.jobs.submitter.submit_one", new=AsyncMock(return_value={"status": "dry_run"})) as one,
        patch("core.jobs.submit.browser.preflight", new=AsyncMock(return_value=_ready(True))),
        patch("core.jobs.submitter._announce", new=AsyncMock()),
    ):
        assert await submitter.run(cfg={"dry_run": True, "per_run": 5}) == {"dry_run": 1}
    one.assert_awaited_once()


# ------------------------------------------------------------------ zapier tools


async def test_mcp_tools_finds_a_server_whatever_its_case(monkeypatch):
    from core import connect

    monkeypatch.setattr(connect, "_TOOLS_CACHE", {})
    saved = json.dumps([{"name": "Zapier", "url": "https://mcp.zapier.com/api/mcp/mcp", "token": "t"}])
    with (
        patch("core.connect.envfile.get", return_value=saved),
        patch("core.connect.check_mcp", new=AsyncMock(return_value={"server": "zapier", "count": 1, "tools": ["gmail_find_email"]})),
    ):
        out = await connect.mcp_tools("zapier")
    assert out["groups"][0]["app"] == "Gmail"

    with patch("core.connect.envfile.get", return_value="[]"), pytest.raises(LookupError, match="add it again in Settings, Connections"):
        await connect.mcp_tools("zapier")


# ------------------------------------------------------------------ the API


@pytest.fixture
def client():
    async def fake_get_pool():
        class P:
            async def close(self): pass
        return P()

    async def fake_close_pool():
        pass

    with (
        patch("core.web.api.db.get_pool", new=fake_get_pool),
        patch("core.web.api.db.close_pool", new=fake_close_pool),
    ):
        from core.web.api import app
        with TestClient(app) as c:
            yield c


def test_setup_route_serves_the_checklist_and_rechecks_on_request(client):
    sheet = {"items": [], "done": 0, "total": 0, "complete": True}
    with (
        patch("core.setup_checklist.checklist", new=AsyncMock(return_value=sheet)),
        patch("core.jobs.submit.browser.preflight", new=AsyncMock(return_value=_ready(True))) as pre,
    ):
        assert client.get("/api/setup").json() == sheet
        pre.assert_not_awaited()
        assert client.get("/api/setup?recheck=true").status_code == 200
        pre.assert_awaited_once_with(fresh=True)


def _health(client, devices: int, loaded: str = "aaa", on_disk: str = "aaa"):
    from datetime import UTC, datetime

    async def fv(q, *a):
        if "push_subscriptions" in q:
            return devices
        return 1 if q == "select 1" else True

    async def beats():
        return {"loop/executive": datetime.now(UTC)}

    slo = {"days": 7, "brief_within_min": 15, "ok": True, "objectives": []}
    with (
        patch("core.web.api.db.fetchval", new=fv),
        patch("core.web.api.all_heartbeats", new=beats),
        patch("core.web.api.settings.vapid_public_key", "pub"),
        patch("core.web.api.settings.vapid_private_key", "priv"),
        patch("core.web.api._google_consent_expired", new=AsyncMock(return_value=False)),
        patch("core.web.api.slo_summary", new=AsyncMock(return_value=slo)),
        patch("core.web.api.BOOT_REVISION", loaded),
        patch("core.web.api.code_revision", return_value=on_disk),
    ):
        return client.get("/api/health").json()


def test_the_push_warning_clears_once_a_device_subscribes(client):
    before = _health(client, devices=0)
    assert any("No phone is subscribed" in w for w in before.get("warnings", []))
    after = _health(client, devices=1)
    assert not after.get("warnings")


def test_health_reports_the_slo(client):
    out = _health(client, devices=1)
    assert out["slo"]["brief_within_min"] == 15


def test_health_says_when_the_web_app_runs_older_code_than_the_checkout(client):
    out = _health(client, devices=1, loaded="aaaaaaa1111", on_disk="bbbbbbb2222")
    assert any("older code (aaaaaaa)" in w and "deploy.bat" in w for w in out["warnings"])
    assert out["code"] == {"loaded": "aaaaaaa1111", "on_disk": "bbbbbbb2222"}
