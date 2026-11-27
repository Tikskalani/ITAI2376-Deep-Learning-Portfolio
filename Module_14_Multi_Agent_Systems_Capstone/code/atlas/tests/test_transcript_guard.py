"""fetch_transcript hands a model-chosen URL to yt-dlp, so it is held to the
fetch_url rule: http(s) on a public host only, and yt-dlp never runs its
generic extractor (the one that GETs any page it is handed, following
redirects fetch_url's hop check cannot see).

DNS is a fake table and yt-dlp is replaced by a recorder, so the tests
prove a refused URL never reached a downloader.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import core.transcription as transcription

DNS: dict[str, list[str]] = {
    "www.youtube.com": ["142.250.80.46"],
    "www.instagram.com": ["157.240.229.174"],
    "intranet.example.com": ["10.1.2.3"],
}


async def _fake_resolve(host: str, port: int) -> list[str]:
    if host not in DNS:
        raise OSError(f"no such host {host}")
    return DNS[host]


class _Recorder:
    """Stands in for yt_dlp.YoutubeDL: records the options and URLs it got."""

    calls: list[tuple[dict[str, Any], list[str]]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    def __enter__(self) -> _Recorder:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def download(self, urls: list[str]) -> None:
        _Recorder.calls.append((self.opts, urls))


@pytest.fixture
def downloads():
    _Recorder.calls = []
    with patch("core.web._resolve", new=_fake_resolve), \
            patch("yt_dlp.YoutubeDL", new=_Recorder), \
            patch("core.youtube.fetch_transcript", return_value=None):
        yield _Recorder.calls


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/briefs",
        "http://localhost:11435/api/tags",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://intranet.example.com/video.mp4",
        "http://[64:ff9b::7f00:1]/",
        "file:///C:/Users/you/Projects/atlas/.env",
        "ftp://example.com/x.mp4",
        "not a url",
    ],
)
async def test_refused_urls_never_reach_the_downloader(downloads, url):
    assert await transcription.get_transcript(url) is None
    assert await transcription.transcribe_url(url) is None
    assert downloads == []


async def test_a_public_video_downloads_without_the_generic_extractor(downloads):
    with patch.object(transcription, "_transcribe_file", return_value=None):
        await transcription.transcribe_url("https://www.instagram.com/reel/abc123/")
    assert len(downloads) == 1
    opts, urls = downloads[0]
    assert urls == ["https://www.instagram.com/reel/abc123/"]
    assert opts["allowed_extractors"] == ["default", "-generic"]


def test_the_real_ytdlp_drops_the_generic_extractor():
    # The option is yt-dlp's own syntax; prove it does what the comment says
    # on the installed version, without touching the network.
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "allowed_extractors": transcription.YTDLP_EXTRACTORS}) as ydl:
        names = {ie.lower() for ie in ydl._ies}
    assert "generic" not in names
    assert "youtube" in names and "instagram" in names


async def test_fetch_transcript_tool_reports_a_refusal_plainly(downloads):
    from agents.news_discovery.tools import fetch_transcript_tool

    out = await fetch_transcript_tool().handler({"url": "http://127.0.0.1:8000/api/briefs"})
    assert out["transcript"] is None
    assert downloads == []
