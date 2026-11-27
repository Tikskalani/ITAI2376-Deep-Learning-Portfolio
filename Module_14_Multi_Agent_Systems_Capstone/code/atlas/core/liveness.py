"""Atlas -- the worker's dead-man switch.

A worker that crashes is restarted by NSSM. A worker that hangs is not: the
process stays up, nothing ticks, and before this module the only thing that
noticed was a health job running inside that same stuck process. This is a
plain thread, independent of the event loop, that ends the process when
either of two things stops moving:

  pulse   an asyncio task touches it every 30 s. If it goes quiet for
          PULSE_LIMIT_S, the event loop itself is blocked (a sync call, a
          deadlock); nothing async can recover from that.
  cycle   run_executive_cycle calls cycle_done() after every tick. If no
          tick completes within the loop interval times two plus a margin
          (counted from start), the loop is stuck in a way the per-stage
          timeouts did not catch.

Both are timed on an awake clock: a laptop asleep for three hours did not
block anything, and counting its sleep used to kill a healthy worker on
every wake (2026-10-19 18:15, "event loop blocked for 2297 s"). The pulse
also notices a wake (the wall clock ran ahead of the awake clock) and hands
the sleep to on_resume, so missed briefs run and say why they are late.

On either, it writes every thread's stack to the log (faulthandler) and
exits with code 70. NSSM restarts the service five seconds later, the new
worker writes worker/started, and the web watchdog announces the recovery.

Only scripts/run_worker.py calls start(); tests and the web process never
arm it.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from core.config import settings

log = logging.getLogger(__name__)

PULSE_EVERY_S = 30.0
PULSE_LIMIT_S = 10 * 60.0
CYCLE_MARGIN_S = 20 * 60.0
EXIT_CODE = 70

# A gap between the wall clock and the awake clock longer than this is a sleep.
SLEEP_MIN_S = 120.0
# A watch-thread sleep that took this many times longer than asked was a suspend.
SUSPEND_FACTOR = 3.0


def _awake() -> float:
    """Seconds of awake time. Sleep and hibernation do not count: on Windows
    time.monotonic() runs on through a suspend, QueryUnbiasedInterruptTime
    does not; on Linux and macOS time.monotonic() already stops."""
    if sys.platform == "win32":
        import ctypes

        ticks = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks)):  # type: ignore[attr-defined]
            return ticks.value / 10_000_000
    return time.monotonic()


_state: dict[str, float | bool] = {"pulse": _awake(), "cycle": _awake(), "armed": False,
                                   "wall": time.time(), "awake": _awake()}
_lock = threading.Lock()


def cycle_limit_s() -> float:
    return settings.loop_interval_min * 2 * 60 + CYCLE_MARGIN_S


def pulse() -> None:
    with _lock:
        _state["pulse"] = _awake()


def cycle_done() -> None:
    with _lock:
        _state["cycle"] = _awake()


def slept(wall: float | None = None, awake: float | None = None) -> tuple[float, float] | None:
    """(asleep_from, asleep_to) as wall-clock seconds when the machine slept
    since the last call, else None. Pure apart from the remembered clocks."""
    wall = time.time() if wall is None else wall
    awake = _awake() if awake is None else awake
    with _lock:
        lost = (wall - float(_state["wall"])) - (awake - float(_state["awake"]))
        _state["wall"], _state["awake"] = wall, awake
    if lost < SLEEP_MIN_S:
        return None
    return wall - lost, wall


def check(now: float | None = None) -> str | None:
    """Why the worker should die now, or None. Pure; the thread calls it."""
    now = _awake() if now is None else now
    with _lock:
        quiet_pulse = now - float(_state["pulse"])
        quiet_cycle = now - float(_state["cycle"])
    if quiet_pulse > PULSE_LIMIT_S:
        return f"event loop blocked for {int(quiet_pulse)} s"
    if quiet_cycle > cycle_limit_s():
        return f"no executive tick completed in {int(quiet_cycle / 60)} min"
    return None


def _die(reason: str) -> None:
    log.critical("liveness: %s; exiting so the service manager restarts the worker", reason)
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - dying anyway
        pass
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(EXIT_CODE)


def suspended(asked_s: float, took_s: float) -> bool:
    """True when the watch thread's own sleep ran far past what it asked for.

    A blocked event loop does not slow this thread's sleep, so a long one means
    the whole process was suspended. On this laptop's standby the awake clock
    kept counting through it (2026-10-21/22: "event loop blocked for 1843 s",
    4686 s, 7065 s, each on a wake), so the awake clock alone cannot tell."""
    return took_s > asked_s * SUSPEND_FACTOR


def _watch(period_s: float) -> None:
    while True:
        before = time.time()
        time.sleep(period_s)
        took = time.time() - before
        if suspended(period_s, took):
            # Woken from a suspend: the quiet time is the machine's, not the loop's.
            now = _awake()
            with _lock:
                _state.update(pulse=now, cycle=now)
            log.info("liveness: the process was suspended about %d min; not counting it", took / 60)
            continue
        reason = check()
        if reason:
            _die(reason)


OnResume = Callable[[datetime, datetime], Awaitable[None]]


async def _pulse_forever(on_resume: OnResume | None = None) -> None:
    while True:
        pulse()
        gap = slept()
        if gap is not None:
            frm, to = (datetime.fromtimestamp(t, UTC) for t in gap)
            log.info("liveness: the machine slept %d min; resuming", (gap[1] - gap[0]) / 60)
            if on_resume is not None:
                try:
                    await on_resume(frm, to)
                except Exception:  # noqa: BLE001 - a failed hook must not stop the pulse
                    log.exception("liveness: on_resume failed")
        await asyncio.sleep(PULSE_EVERY_S)


def start(check_every_s: float = 60.0, on_resume: OnResume | None = None) -> asyncio.Task:
    """Arm the switch. Call once from the worker's event loop. on_resume is
    awaited with (asleep_from, asleep_to) after the machine wakes."""
    now = _awake()
    with _lock:
        _state.update(pulse=now, cycle=now, armed=True, wall=time.time(), awake=now)
    threading.Thread(target=_watch, args=(check_every_s,), name="atlas-liveness", daemon=True).start()
    log.info(
        "liveness armed: restart if the event loop blocks %d min or no tick completes in %d min",
        PULSE_LIMIT_S / 60,
        cycle_limit_s() / 60,
    )
    return asyncio.get_running_loop().create_task(_pulse_forever(on_resume))


__all__ = ["check", "cycle_done", "cycle_limit_s", "pulse", "slept", "start", "suspended"]
