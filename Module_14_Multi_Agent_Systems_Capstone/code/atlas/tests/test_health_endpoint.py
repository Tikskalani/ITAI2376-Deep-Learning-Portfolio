"""GET /api/health: what the deploy script and an uptime monitor read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.config import REPO_ROOT


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


def _patch_db(fetchval, beats):
    async def all_beats():
        if isinstance(beats, Exception):
            raise beats
        return beats

    return (
        patch("core.web.api.db.fetchval", new=fetchval),
        patch("core.web.api.all_heartbeats", new=all_beats),
    )


async def _fv_ok(q, *a):
    if q == "select 1":
        return 1
    if "push_subscriptions" in q:
        return 1
    return True


def _now():
    return datetime.now(UTC)


def test_health_ok_with_fresh_worker(client):
    a, b = _patch_db(_fv_ok, {"loop/executive": _now() - timedelta(minutes=3)})
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    from core.version import VERSION
    assert body["ok"] is True and body["status"] == "ok"
    assert body["version"] == VERSION
    assert body["db"] == "ok"
    assert body["schema"] == "current"
    assert body["worker"]["fresh"] is True
    assert body["worker"]["state"] == "ticking"
    assert 2 <= body["worker"]["age_min"] <= 4
    assert body["problems"] == []
    assert body["push_devices"] == 1


def test_health_worker_never_ran_is_degraded(client):
    a, b = _patch_db(_fv_ok, {})
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and body["ok"] is False
    assert body["worker"]["state"] == "never"
    assert "never run" in body["problems"][0]


def test_health_stale_worker_fails_with_a_reason(client):
    """The audit case: the loop dead for 108 minutes must not read ok."""
    a, b = _patch_db(_fv_ok, {"loop/executive": _now() - timedelta(minutes=108)})
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["status"] == "degraded"
    assert body["worker"]["fresh"] is False and body["worker"]["state"] == "stalled"
    assert "108 minutes" in body["problems"][0]


def test_health_web_scope_ignores_the_loop(client):
    """deploy.ps1 waits on ?web=1: it restarts both services together."""
    a, b = _patch_db(_fv_ok, {"loop/executive": _now() - timedelta(hours=6)})
    with a, b:
        r = client.get("/api/health?web=1")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_health_restarted_worker_counts_as_starting(client):
    beats = {
        "loop/executive": _now() - timedelta(hours=6),
        "worker/started": _now() - timedelta(minutes=4),
    }
    a, b = _patch_db(_fv_ok, beats)
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["worker"]["state"] == "starting"


def test_health_warns_when_no_phone_gets_push(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_private_key", "priv")

    async def fv(q, *a):
        return 0 if "push_subscriptions" in q else await _fv_ok(q)

    a, b = _patch_db(fv, {"loop/executive": _now()})
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert "Settings, Connections" in r.json()["warnings"][0] and "Phone push" in r.json()["warnings"][0]


def test_health_503_when_schema_behind(client):
    async def fetchval(q, *a):
        return 1 if q == "select 1" else (0 if "push" in q else False)

    a, b = _patch_db(fetchval, {"loop/executive": _now()})
    with a, b:
        r = client.get("/api/health?web=1")
    assert r.status_code == 503
    assert r.json()["schema"] == "behind"
    assert r.json()["status"] == "down"


def test_health_503_when_db_down(client):
    async def fetchval(q, *a):
        raise OSError("connection refused")

    a, b = _patch_db(fetchval, AssertionError("not reached"))
    with a, b:
        r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["db"] == "down"
    assert r.json()["problems"] == ["The database is not answering."]


def test_schema_probe_tracks_the_newest_migration():
    from core.web.api import SCHEMA_PROBE

    migrations = sorted(Path(REPO_ROOT, "db", "migrations").glob("*.sql"))
    newest = migrations[-1]
    assert newest.name == SCHEMA_PROBE[0], (
        f"new migration {newest.name}: point SCHEMA_PROBE in core/web/api.py at an object it creates"
    )
    assert SCHEMA_PROBE[1] in newest.read_text(encoding="utf-8")


def test_dashboard_version_matches():
    from core.version import VERSION

    html = Path(REPO_ROOT, "core", "web", "static", "index.html").read_text(encoding="utf-8")
    assert f"v{VERSION}" in html
