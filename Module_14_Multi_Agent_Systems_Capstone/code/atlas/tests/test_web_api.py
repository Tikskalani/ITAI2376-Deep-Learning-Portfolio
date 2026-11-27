"""Minimal smoke tests for the FastAPI web surface.

Tests that routes are registered and OpenAPI compiles. The DB layer is
patched so we don't need a running Postgres. Per-endpoint behavior is
covered by the underlying module's tests (costs, decisions_review).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


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


def test_dashboard_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "atlas" in r.text.lower()


def test_openapi_lists_expected_paths(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = set(r.json()["paths"].keys())
    assert {"/api/chat", "/api/cost", "/api/episodes", "/api/decisions", "/api/entities"} <= paths


def test_chat_rejects_empty_message(client):
    r = client.post("/api/chat", json={"message": "  "})
    assert r.status_code == 400
