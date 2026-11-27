"""describe_api_error: the sentence the dashboard shows when a Claude call fails."""

from __future__ import annotations

import anthropic
import pytest

from core.llm import describe_api_error

try:  # anthropic 1.x is built on httpx2; 0.x on httpx
    import httpx2 as _http
except ImportError:  # pragma: no cover
    import httpx as _http

_REQ = _http.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(cls, code: int, message: str):
    body = {"type": "error", "error": {"type": "x", "message": message}}
    return cls(message, response=_http.Response(code, request=_REQ), body=body)


def test_bad_key_names_the_fix():
    text = describe_api_error(_status(anthropic.AuthenticationError, 401, "invalid x-api-key"))
    assert "401" in text and "ANTHROPIC_API_KEY" in text and "deploy.bat" in text


def test_low_credit_keeps_anthropics_message():
    msg = "Your credit balance is too low to access the Anthropic API."
    text = describe_api_error(_status(anthropic.BadRequestError, 400, msg), "claude-opus-5-5")
    assert text.startswith("Anthropic rejected the request (400)")
    assert "credit balance is too low" in text


def test_model_access_names_the_model():
    text = describe_api_error(_status(anthropic.NotFoundError, 404, "model: claude-opus-5-5"), "claude-opus-5-5")
    assert "claude-opus-5-5" in text and "404" in text
    text = describe_api_error(_status(anthropic.PermissionDeniedError, 403, "no access"), "claude-opus-5-5")
    assert "not allowed to use claude-opus-5-5" in text


def test_rate_limit_and_server_errors():
    assert "429" in describe_api_error(_status(anthropic.RateLimitError, 429, "slow down"))
    assert "server error (529)" in describe_api_error(_status(anthropic.APIStatusError, 529, "overloaded"))


def test_network_failure():
    text = describe_api_error(anthropic.APIConnectionError(request=_REQ))
    assert "api.anthropic.com" in text


def test_long_messages_are_trimmed_and_unknown_errors_named():
    text = describe_api_error(_status(anthropic.BadRequestError, 400, "x" * 2000))
    assert len(text) < 320
    assert describe_api_error(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.parametrize("deep_ok", [True, False])
def test_deep_health_reports_claude_without_gating(deep_ok):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    async def fake_get_pool():
        class P:
            async def close(self): pass
        return P()

    async def fake_close_pool():
        pass

    async def fetchval(q, *a):
        return 1 if q == "select 1" else True

    async def beats():
        from datetime import UTC, datetime

        return {"loop/executive": datetime.now(UTC)}

    async def check():
        return {"ok": deep_ok, "detail": "key accepted" if deep_ok else "Anthropic rejected the API key (401)."}

    with (
        patch("core.web.api.db.get_pool", new=fake_get_pool),
        patch("core.web.api.db.close_pool", new=fake_close_pool),
        patch("core.web.api.db.fetchval", new=fetchval),
        patch("core.web.api.all_heartbeats", new=beats),
        patch("core.web.api._anthropic_check", new=check),
    ):
        from core.web.api import app
        with TestClient(app) as c:
            plain = c.get("/api/health").json()
            deep = c.get("/api/health?deep=1")
    assert "anthropic" not in plain
    assert deep.status_code == 200
    assert deep.json()["anthropic"]["ok"] is deep_ok
