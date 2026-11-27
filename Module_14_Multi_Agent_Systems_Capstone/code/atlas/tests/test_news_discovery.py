"""Unit tests for the News & Discovery agent.

The pure HTML helpers are tested directly. fetch_url is exercised against
an httpx.MockTransport and a fake DNS answer, so no real network call is
made and no extra dep (respx / httpx_mock) is required.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from agents.news_discovery import register_news_discovery
from agents.news_discovery.tools import build_tools as nd_build_tools
from core.runtime.dispatcher import get_registration
from core.web import extract_title, html_to_text


def _nd_tool(name: str):
    return {t.name: t for t in nd_build_tools()}[name].handler


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_news_discovery_wires_tools():
    register_news_discovery()
    reg = get_registration("news_discovery")
    assert reg is not None
    assert reg.model_tier == "specialist"
    assert reg.tools_factory is not None
    names = {t.name for t in reg.tools_factory()}
    assert "fetch_url" in names
    assert "record_lesson" in names


def test_news_discovery_toolset():
    names = {t.name for t in nd_build_tools()}
    assert names == {
        "fetch_url",
        "fetch_transcript",
        "propose_learning",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
        "record_lesson",
    }


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def test_html_to_text_strips_scripts_styles_and_tags():
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert(1)</script>"
        "<h1>Title</h1><p>The body has <b>bold</b> text.</p>"
        "</body></html>"
    )
    out = html_to_text(html)
    assert "Title" in out
    assert "bold" in out
    assert "alert" not in out
    assert "color:red" not in out


def test_html_to_text_decodes_common_entities():
    out = html_to_text("<p>Tom &amp; Jerry &mdash; classic</p>")
    assert "Tom & Jerry" in out


def testextract_title():
    assert extract_title("<html><head><title> Hello World </title></head>") == "Hello World"
    assert extract_title("<html><head></head></html>") is None


# ---------------------------------------------------------------------------
# fetch_url (an httpx.MockTransport stands in for the network; the SSRF
# guard itself is covered in tests/test_web_fetch_guard.py)
# ---------------------------------------------------------------------------


@contextmanager
def _serving(body: bytes, *, content_type: str = "text/html; charset=utf-8", status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    async def public_dns(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    with patch("core.web._transport", return_value=httpx.MockTransport(handler)), \
            patch("core.web._resolve", new=public_dns):
        yield


async def test_fetch_url_happy_path():
    body = (
        b"<html><head><title>Cache Invalidation</title></head>"
        b"<body><script>noisy()</script><p>Two hard things.</p></body></html>"
    )
    with _serving(body):
        out = await _nd_tool("fetch_url")({"url": "https://example.com/post"})

    assert out["title"] == "Cache Invalidation"
    assert "Two hard things" in out["content"]
    assert "noisy()" not in out["content"]
    assert out["truncated"] is False
    assert out["url"] == "https://example.com/post"


async def test_fetch_url_truncates_long_pages():
    # Build a long body that exceeds the 30 000 char cap after cleanup.
    chunk = b"<p>The quick brown fox jumped over the lazy dog. </p>"
    body = b"<html><body>" + (chunk * 1000) + b"</body></html>"
    with _serving(body):
        out = await _nd_tool("fetch_url")({"url": "https://example.com/x"})
    assert out["truncated"] is True
    assert len(out["content"]) <= 30_000


async def test_fetch_url_rejects_empty():
    out = await _nd_tool("fetch_url")({"url": "   "})
    assert "error" in out


async def test_fetch_url_rejects_non_http_scheme():
    out = await _nd_tool("fetch_url")({"url": "ftp://example.com/x"})
    assert "error" in out


async def test_fetch_url_rejects_unsupported_content_type():
    with _serving(b"%PDF-1.4...", content_type="application/pdf"):
        out = await _nd_tool("fetch_url")({"url": "https://example.com/x.pdf"})
    assert "error" in out
    assert "application/pdf" in out["error"]


async def test_fetch_url_propagates_http_error():
    with _serving(b"<html>nope</html>", status=503):
        out = await _nd_tool("fetch_url")({"url": "https://example.com/x"})
    assert "error" in out
    assert "503" in out["error"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
