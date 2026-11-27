"""The worker watching the web app (core/loop/webwatch.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from core import db
from core.loop import webwatch
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    webwatch.reset()
    async with livedb.shadow(monkeypatch, "notifications", "push_subscriptions", "events"):
        yield
    webwatch.reset()


def _probe(answers: list[bool]):
    async def probe():
        return answers.pop(0)
    return probe


def _restart(result: str = "restarted"):
    calls: list[str] = []

    async def restart(name):
        calls.append(name)
        return result
    restart.calls = calls  # type: ignore[attr-defined]
    return restart


async def _titles() -> list[str]:
    return [r["title"] for r in await db.fetch("select title from notifications order by created_at")]


@aio
async def test_three_misses_restart_once_per_cooldown_and_recovery_is_announced(world):
    probe = _probe([False, False, False, False, False, True, True])
    restart = _restart()
    t = 1_000_000.0
    assert (await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t))["action"] == "none"
    assert (await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 120))["action"] == "none"
    assert restart.calls == [] and await _titles() == []
    out = await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 240)
    assert out == {"ok": False, "misses": 3, "action": "restarted"} and restart.calls == ["AtlasWeb"]
    assert await _titles() == ["Atlas restarted the web app"]
    # Still down inside the cooldown: no second restart, no second card.
    assert (await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 360))["action"] == "waiting"
    assert (await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 480))["action"] == "waiting"
    assert restart.calls == ["AtlasWeb"] and len(await _titles()) == 1
    # Back: one card saying so, counters reset.
    assert await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 600) == {"ok": True, "misses": 0, "action": "none"}
    assert await _titles() == ["Atlas restarted the web app", "Atlas web app is back"]
    assert await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 720) == {"ok": True, "misses": 0, "action": "none"}
    assert len(await _titles()) == 2


@aio
async def test_after_the_cooldown_a_still_dead_app_is_restarted_again(world):
    probe = _probe([False] * 4)
    restart = _restart()
    t = 2_000_000.0
    for i in range(3):
        await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + 120 * i)
    assert restart.calls == ["AtlasWeb"]
    out = await webwatch.tick(probe_fn=probe, restart_fn=restart, now=t + webwatch.COOLDOWN_S + 1000)
    assert out["action"] == "restarted" and restart.calls == ["AtlasWeb", "AtlasWeb"]


@aio
async def test_when_it_cannot_restart_it_says_so_and_points_at_deploy(world):
    probe = _probe([False] * 3)
    restart = _restart("missing")
    for i in range(3):
        out = await webwatch.tick(probe_fn=probe, restart_fn=restart, now=3_000_000.0 + 120 * i)
    assert out["action"] == "missing"
    row = await db.fetchrow("select title, body, priority from notifications")
    assert row["title"] == "Atlas web app is down" and "deploy.bat" in row["body"] and row["priority"] == "high"

    async def boom(name):
        raise RuntimeError("sc.exe not found")
    webwatch.reset()
    probe = _probe([False] * 3)
    for i in range(3):
        out = await webwatch.tick(probe_fn=probe, restart_fn=boom, now=4_000_000.0 + 120 * i)
    assert out["action"].startswith("failed: RuntimeError")


@aio
async def test_the_probe_asks_whether_the_web_app_answers_not_whether_the_system_is_well(world):
    """A degraded health answer (503 because the worker stalled) is a live web
    app; restarting it for that would be the worker chasing its own tail."""
    import json

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            body = {"ok": False, "status": "degraded", "version": "0.17.0", "db": "ok",
                    "problems": ["The background loop has not run for 74 minutes."]}
            return httpx.Response(503, json=body)
        if path == "/fine/api/health":
            return httpx.Response(200, json={"ok": True, "status": "ok", "version": "0.17.0", "db": "ok"})
        if path == "/html/api/health":
            return httpx.Response(200, text="<html>a proxy's error page</html>", headers={"content-type": "text/html"})
        if path == "/boom/api/health":
            return httpx.Response(500, text="Internal Server Error")
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    assert await webwatch.probe("http://web", transport=transport) is True          # degraded, but answering
    assert await webwatch.probe("http://web/fine", transport=transport) is True
    assert await webwatch.probe("http://web/html", transport=transport) is False    # not Atlas answering
    assert await webwatch.probe("http://web/boom", transport=transport) is False
    assert await webwatch.probe("http://web/gone", transport=transport) is False    # connection refused
    assert json.dumps({"version": "x"})  # the body shape the probe looks for


@aio
async def test_a_probe_that_raises_counts_as_a_miss(world):
    async def broken():
        raise OSError("no route")
    assert (await webwatch.tick(probe_fn=broken, restart_fn=_restart(), now=5_000_000.0))["misses"] == 1


def test_the_windows_restart_recipe(monkeypatch):
    """sc stop, wait for STOPPED (end the process if it will not), sc start."""
    seen: list[list[str]] = []
    state = {"queries": 0}

    def fake_run(args, **kw):
        seen.append(list(args))
        if args[:2] == ["sc.exe", "query"]:
            state["queries"] += 1
            # Running at first; still running after two polls; then stopped.
            return SimpleNamespace(returncode=0, stdout="STATE : 4 RUNNING" if state["queries"] < 4 else "STATE : 1 STOPPED", stderr="")
        if args[:2] == ["sc.exe", "queryex"]:
            return SimpleNamespace(returncode=0, stdout="PID : 4242", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(webwatch.subprocess, "run", fake_run)
    monkeypatch.setattr(webwatch.time, "sleep", lambda s: None)
    assert webwatch._restart_windows_service("AtlasWeb") == "restarted"
    assert ["sc.exe", "stop", "AtlasWeb"] in seen and ["sc.exe", "start", "AtlasWeb"] in seen
    assert not any(a[0] == "taskkill.exe" for a in seen)          # it stopped on its own

    # A service that never stops is ended by pid, then started.
    seen.clear()
    clock = {"t": 0.0}
    monkeypatch.setattr(webwatch.time, "time", lambda: clock.__setitem__("t", clock["t"] + 10) or clock["t"])

    def stubborn(args, **kw):
        seen.append(list(args))
        if args[:2] == ["sc.exe", "query"]:
            return SimpleNamespace(returncode=0, stdout="STATE : 4 RUNNING", stderr="")
        if args[:2] == ["sc.exe", "queryex"]:
            return SimpleNamespace(returncode=0, stdout="        PID                : 4242", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(webwatch.subprocess, "run", stubborn)
    assert webwatch._restart_windows_service("AtlasWeb") == "restarted"
    assert ["taskkill.exe", "/PID", "4242", "/T", "/F"] in seen

    # Not installed, and a start that fails.
    monkeypatch.setattr(webwatch.subprocess, "run",
                        lambda args, **kw: SimpleNamespace(returncode=1060, stdout="[SC] OpenService FAILED 1060", stderr=""))
    assert webwatch._restart_windows_service("AtlasWeb") == "missing"

    def start_fails(args, **kw):
        if args[:2] == ["sc.exe", "start"]:
            return SimpleNamespace(returncode=1, stdout="[SC] StartService FAILED 1053", stderr="")
        return SimpleNamespace(returncode=0, stdout="STATE : 1 STOPPED", stderr="")

    monkeypatch.setattr(webwatch.subprocess, "run", start_fails)
    assert webwatch._restart_windows_service("AtlasWeb").startswith("failed: [SC] StartService FAILED 1053")
