"""Suite-wide defaults.

The write guard (core/web/guard.py) refuses API writes without the
dashboard's X-Atlas-Client header. Route tests exercise the routes, not the
guard, so it is off by default here; tests/test_web_guard.py turns it on.
The web watchdog is off so no background task outlives a test client.

Locally .env holds real VAPID keys, SMTP settings and the healthchecks.io
URL, so a test that reaches notify() or the loop's ping would reach the
phone, the inbox or the alarm. All three are blanked here; a test that
exercises delivery sets fake values itself and installs fake senders.
"""

from __future__ import annotations

import pytest

from core.config import settings


@pytest.fixture(autouse=True)
def _suite_defaults(monkeypatch):
    monkeypatch.setattr(settings, "write_guard", False)
    monkeypatch.setattr(settings, "web_watchdog", False)
    monkeypatch.setattr(settings, "vapid_private_key", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "heartbeat_url", "")
    # Tapping Apply starts the job submitter in the background. A test that taps
    # it must not leave a run behind that reads the live queue after it ends.
    from core.web import jobs as jobs_routes

    monkeypatch.setattr(jobs_routes, "_start_submit", _no_background_submit)
    # Every run, tool call, approval and push announces itself on the event
    # stream. On the live database that is a real row per announcement, so
    # the stream is silent in tests unless a test asks for it (tests/test_events.py).
    from core import events

    monkeypatch.setattr(events, "emit", _silent_emit)
    # The privacy gate reads data/model_exclude.yaml, Trilok's real list on the
    # laptop. Tests never see it: no rules and safe-start, unless a test sets its own.
    from core import privacy

    monkeypatch.setattr(privacy, "RULES_PATH", privacy.REPO_ROOT / "tests" / "no-such-rules.yaml")
    monkeypatch.setattr(settings, "mail_model_reads", False)
    privacy.reset_cache()


REAL_EMIT = None  # set below, before any test patches it


async def _silent_emit(kind, **_kw):
    return None


def _no_background_submit(coro) -> bool:
    coro.close()
    return False


from core import events as _events  # noqa: E402

REAL_EMIT = _events.emit


LIVE_BEFORE = pytest.StashKey[dict[str, str] | None]()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Hash every live table before any test runs (tests/livedb.py drift guard)."""
    from tests import livedb

    session.config.stash[LIVE_BEFORE] = livedb.snapshot()
