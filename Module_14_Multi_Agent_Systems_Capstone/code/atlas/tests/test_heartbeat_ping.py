"""Tests for the external dead-man heartbeat ping (core/loop/executive.py).

The ping must never raise, must be a no-op when unconfigured, and must
report success only on a 2xx. httpx is mocked with its own MockTransport
so no network is touched.
"""

from __future__ import annotations

import httpx
import pytest

from core.loop import executive


@pytest.fixture
def mock_transport(monkeypatch):
    """Route httpx.AsyncClient through a MockTransport; returns a recorder."""
    calls: list[httpx.Request] = []
    status_holder = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_holder["code"])

    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(executive.httpx, "AsyncClient", patched_client)
    return calls, status_holder


async def test_ping_disabled_when_url_blank(mock_transport):
    calls, _ = mock_transport
    assert await executive.ping_external_heartbeat("") is False
    assert await executive.ping_external_heartbeat("   ") is False
    assert calls == []


async def test_ping_success_on_2xx(mock_transport):
    calls, _ = mock_transport
    ok = await executive.ping_external_heartbeat("https://hc-ping.com/abc")
    assert ok is True
    assert len(calls) == 1
    assert str(calls[0].url) == "https://hc-ping.com/abc"


async def test_ping_reports_false_on_non_2xx(mock_transport):
    calls, status = mock_transport
    status["code"] = 500
    assert await executive.ping_external_heartbeat("https://hc-ping.com/abc") is False
    assert len(calls) == 1


async def test_ping_never_raises_on_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(executive.httpx, "AsyncClient", patched_client)
    assert await executive.ping_external_heartbeat("https://hc-ping.com/abc") is False


async def test_ping_uses_settings_url_by_default(mock_transport, monkeypatch):
    calls, _ = mock_transport
    monkeypatch.setattr(executive.settings, "heartbeat_url", "https://hc-ping.com/from-settings")
    assert await executive.ping_external_heartbeat() is True
    assert str(calls[0].url) == "https://hc-ping.com/from-settings"
