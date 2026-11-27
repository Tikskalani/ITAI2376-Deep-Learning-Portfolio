"""Atlas -- the worker watches the web app.

core.loop.health is the web process watching the worker. Nobody watched
the web process: on 2026-10-09 it hung after an out-of-memory start and
the dashboard was dead until Trilok ran deploy.bat. This is the other half.

Every two minutes the worker asks /api/health. After three misses in a row
it restarts the AtlasWeb service the way deploy.bat does (stop with a time
limit, end the process if it will not stop, start), tells him once with a
card, and announces the recovery on the event stream. A restart is tried
at most once per cooldown, so a web app that cannot start is not bounced
forever; after that it only alerts.

Restarting a Windows service needs the rights the service account has. The
worker runs under his account (install_windows_service -RunAsCurrentUser),
an administrator, and a service process carries its full token, so no UAC
prompt is involved. Anywhere else, or with the service missing, the
watchdog alerts and leaves the restart to him.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core import events
from core.config import settings

log = logging.getLogger(__name__)

MISSES = 3                 # consecutive failed probes before acting
COOLDOWN_S = 15 * 60       # at most one restart per cooldown
SERVICE = "AtlasWeb"
_PID = re.compile(r"PID\s*:\s*(\d+)")


@dataclass
class State:
    misses: int = 0
    last_restart: float = 0.0
    alerted: bool = False


_state = State()


def reset() -> None:
    """Forget the run so far (tests)."""
    global _state
    _state = State()


PROBE_SECONDS = 5.0


async def probe(url: str | None = None, *, transport: Any = None) -> bool:
    """True when the web app answers /api/health with its own JSON.

    The question is whether the process is alive, not whether the system
    is well: /api/health answers 503 when the worker's loop has stalled
    (the laptop slept), and restarting the web app for that would be the
    worker chasing its own tail. A hang, a refused connection, a 5xx with
    no body, or something else's HTML (a proxy) is a miss."""
    base = (url or settings.web_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=PROBE_SECONDS, transport=transport) as client:
            r = await client.get(f"{base}/api/health")
        body = r.json()
        return isinstance(body, dict) and "version" in body
    except Exception as exc:  # noqa: BLE001 - any failure is a miss
        log.debug("webwatch: probe failed: %s", exc)
        return False


async def tick(*, probe_fn: Any = None, restart_fn: Any = None, now: float | None = None) -> dict[str, Any]:
    """One pass. Returns what it saw and what it did. Never raises."""
    st = _state
    now = time.time() if now is None else now
    try:
        ok = await (probe_fn or probe)()
    except Exception:  # noqa: BLE001
        log.exception("webwatch: probe raised")
        ok = False
    if ok:
        was_down = st.misses >= MISSES
        st.misses = 0
        if was_down:
            log.info("webwatch: the web app is answering again")
            await events.emit("web.recovered", title="The web app is answering again")
            if st.alerted:
                st.alerted = False
                await _say("Atlas web app is back", "The dashboard is answering again.", priority="normal")
        return {"ok": True, "misses": 0, "action": "none"}

    st.misses += 1
    log.warning("webwatch: /api/health missed (%d in a row)", st.misses)
    if st.misses < MISSES:
        return {"ok": False, "misses": st.misses, "action": "none"}
    if now - st.last_restart < COOLDOWN_S:
        return {"ok": False, "misses": st.misses, "action": "waiting"}
    st.last_restart = now
    try:
        result = await (restart_fn or restart_service)(SERVICE)
    except Exception as exc:  # noqa: BLE001
        log.exception("webwatch: restart raised")
        result = f"failed: {type(exc).__name__}: {exc}"
    st.alerted = True
    if result == "restarted":
        await events.emit("web.restarted", title=f"Restarted {SERVICE} after {st.misses} missed health checks")
        await _say("Atlas restarted the web app",
                   f"The dashboard stopped answering ({st.misses} health checks in a row), so the worker restarted the "
                   f"{SERVICE} service. If it keeps happening, the web log under data\\logs says why.")
    else:
        await events.emit("web.down", title=f"The web app is not answering ({result})")
        await _say("Atlas web app is down",
                   f"The dashboard has missed {st.misses} health checks in a row and the worker could not restart it "
                   f"({result}). Run scripts\\deploy.bat to bring it back.")
    return {"ok": False, "misses": st.misses, "action": result}


async def _say(title: str, body: str, *, priority: str = "high") -> None:
    from core.surfaces.notify import notify

    try:
        await notify(kind="health", title=title, body=body, priority=priority)
    except Exception:
        log.exception("webwatch: could not notify")


# ------------------------------------------------------------------ the restart


async def restart_service(name: str = SERVICE) -> str:
    """'restarted', 'missing', 'unsupported', or 'failed: <why>'."""
    if sys.platform != "win32":
        return "unsupported"
    return await asyncio.to_thread(_restart_windows_service, name)


def _sc(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=60, check=False)


def _restart_windows_service(name: str) -> str:
    """deploy.bat's recipe: stop with a limit, end the process if it will not
    stop, start. Not `nssm restart`, which waits on the Service Control
    Manager with no limit."""
    q = _sc("query", name)
    if q.returncode != 0 and "1060" in (q.stdout + q.stderr):
        return "missing"
    if q.returncode != 0:
        return f"failed: {(q.stdout or q.stderr).strip()[:200]}"
    _sc("stop", name)
    deadline = time.time() + 45
    stopped = False
    while time.time() < deadline:
        time.sleep(2)
        if "STOPPED" in _sc("query", name).stdout:
            stopped = True
            break
    if not stopped:
        m = _PID.search(_sc("queryex", name).stdout)
        if m and int(m.group(1)):
            subprocess.run(["taskkill.exe", "/PID", m.group(1), "/T", "/F"], capture_output=True, text=True,
                           timeout=60, check=False)
            time.sleep(3)
    s = _sc("start", name)
    out = (s.stdout + s.stderr).strip()
    if s.returncode != 0 and "1056" not in out:  # 1056: already running
        return f"failed: {out[:200]}"
    return "restarted"


__all__ = ["MISSES", "COOLDOWN_S", "SERVICE", "State", "reset", "probe", "tick", "restart_service"]
