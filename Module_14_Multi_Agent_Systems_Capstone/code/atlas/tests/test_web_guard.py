"""The request guard: cross-origin writes refused, bodies capped, headers set."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.web import guard
from core.web.guard import API_MAX_BYTES, write_refusal

H = {b"host": b"desktop-6o7flb3.taila6289a.ts.net"}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "write_guard", True)
    monkeypatch.setattr(settings, "allowed_origins", "")


def test_reads_are_never_refused(on):
    assert write_refusal("GET", "/api/approvals/pending", {**H, b"origin": b"https://evil.example"}) is None


def test_write_without_the_header_is_refused(on):
    assert "X-Atlas-Client" in write_refusal("POST", "/api/approvals/x/decide", H)


def test_dashboard_write_passes(on):
    hdr = {**H, b"origin": b"https://desktop-6o7flb3.taila6289a.ts.net", b"x-atlas-client": b"1"}
    assert write_refusal("POST", "/api/approvals/x/decide", hdr) is None


def test_forwarded_host_counts_as_same_origin(on):
    hdr = {b"host": b"127.0.0.1:8000", b"x-forwarded-host": b"desktop-6o7flb3.taila6289a.ts.net",
           b"origin": b"https://desktop-6o7flb3.taila6289a.ts.net", b"x-atlas-client": b"1"}
    assert write_refusal("POST", "/api/chat/stream", hdr) is None


def test_cross_origin_write_is_refused_even_with_the_header(on):
    hdr = {**H, b"origin": b"https://evil.example", b"x-atlas-client": b"1"}
    assert "cross-origin" in write_refusal("DELETE", "/api/push/subscribe", hdr)
    hdr[b"origin"] = b"null"
    assert "opaque" in write_refusal("POST", "/api/learn", hdr)


def test_allowed_origins_setting(on, monkeypatch):
    monkeypatch.setattr(settings, "allowed_origins", "https://atlas.example.com")
    hdr = {**H, b"origin": b"https://atlas.example.com", b"x-atlas-client": b"1"}
    assert write_refusal("POST", "/api/learn", hdr) is None


def test_token_routes_skip_the_header_check(on):
    assert write_refusal("POST", "/api/ingest", {**H, b"origin": b"https://evil.example"}) is None


def test_escape_hatch(monkeypatch):
    monkeypatch.setattr(settings, "write_guard", False)
    assert write_refusal("POST", "/api/learn", H) is None


def test_upload_gets_the_bigger_cap():
    assert guard.body_limit("/api/upload") > guard.body_limit("/api/chat") == API_MAX_BYTES


@pytest.fixture
def client():
    async def fake_get_pool():
        class P:
            async def close(self): pass
        return P()

    with (
        patch("core.web.api.db.get_pool", new=fake_get_pool),
        patch("core.web.api.db.close_pool", new=AsyncMock()),
    ):
        from core.web.api import app
        with TestClient(app) as c:
            yield c


def test_end_to_end_refusal_and_security_headers(client, on):
    r = client.post("/api/learn", json={"content": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "cross-origin" in r.json()["detail"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    r = client.get("/api/docs")
    assert r.headers["referrer-policy"] == "no-referrer"


def test_oversized_body_is_refused(client, on):
    big = "x" * (API_MAX_BYTES + 10)
    r = client.post("/api/chat", content=big, headers={"Content-Type": "application/json", "X-Atlas-Client": "1"})
    assert r.status_code == 413


def test_chat_message_length_is_capped(client):
    r = client.post("/api/chat", json={"message": "x" * 200_001})
    assert r.status_code == 422


def test_memory_search_is_free_and_survives_a_failing_half(client):
    facts = AsyncMock(return_value=[{"id": "f1", "content": "Dr. Will owes the shelf count", "source": "mail"}])
    eps = AsyncMock(side_effect=RuntimeError("embedding model down"))
    with patch("core.memory.search_facts", new=facts), patch("core.memory.search_episodes", new=eps):
        r = client.get("/api/memory/search?q=shelf count")
    assert r.status_code == 200
    body = r.json()
    assert body["facts"][0]["content"].startswith("Dr. Will") and body["episodes"] == []
    assert client.get("/api/memory/search?q=a").json()["facts"] == []
