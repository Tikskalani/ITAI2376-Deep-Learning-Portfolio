"""Atlas -- YouTube ingestion helpers for the news_discovery agent.

Two jobs: notice new uploads on a channel (via the channel's public Atom
RSS feed, no API key needed) and pull a video's transcript -- the spoken
content, which is where an AI tip actually lives. Pure parsing is kept
separate from the network calls so it can be tested without hitting
YouTube.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_TIMEOUT = 15.0
_UA = "Mozilla/5.0 (compatible; AtlasNewsDiscovery/1.0)"
_VID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_CHANNEL_ID_RE = re.compile(r'"(?:channelId|externalId)":"(UC[A-Za-z0-9_-]{22})"')


log = logging.getLogger(__name__)


def rss_url(channel_id: str) -> str:
    return _RSS.format(cid=channel_id)


def video_id_from_url(url: str) -> str | None:
    """Pull the 11-char video id out of any common YouTube URL shape."""
    if not url:
        return None
    url = url.strip()
    if _VID_RE.fullmatch(url):
        return url
    for pat in (
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"/shorts/([A-Za-z0-9_-]{11})",
        r"/embed/([A-Za-z0-9_-]{11})",
        r"/live/([A-Za-z0-9_-]{11})",
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def parse_uploads_rss(xml_text: str) -> list[dict[str, Any]]:
    """Parse a YouTube channel Atom feed into upload dicts, in feed order."""
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId")
        if not vid:
            continue
        title = entry.findtext(f"{_ATOM}title") or ""
        published = entry.findtext(f"{_ATOM}published")
        author = entry.findtext(f"{_ATOM}author/{_ATOM}name") or ""
        link_el = entry.find(f"{_ATOM}link")
        href = link_el.get("href") if link_el is not None else None
        out.append({
            "video_id": vid,
            "title": title.strip(),
            "published": published,
            "author": author.strip() or None,
            "url": href or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


async def _get_text(url: str) -> str | None:
    """GET a URL as text, impersonating a real browser (curl_cffi) to dodge
    YouTube's bot-detection, with a plain-httpx fallback. Returns the body on
    a 200, else None. This is what lets the RSS feed fetch stop getting
    soft-blocked with 404/500 the way a bare request does."""
    session_cls: Any = None
    try:
        from curl_cffi.requests import AsyncSession as session_cls
    except ImportError:
        pass
    if session_cls is not None:
        try:
            async with session_cls() as session:
                r = await session.get(url, impersonate="chrome", timeout=_TIMEOUT)
                if r.status_code == 200:
                    return r.text
                log.warning("GET %s -> HTTP %s (impersonated)", url, r.status_code)
        except Exception as e:
            log.warning("impersonated GET error for %s: %s", url, e)

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True
        ) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            log.warning("GET %s -> HTTP %s (plain)", url, r.status_code)
    except httpx.HTTPError as e:
        log.warning("plain GET error for %s: %s", url, e)
    return None


async def resolve_channel_id(handle_or_url: str) -> str | None:
    """Resolve an @handle or channel URL to its UC... channel id by reading the page."""
    h = (handle_or_url or "").strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", h):
        return h
    if h.startswith("http"):
        url = h
    elif h.startswith("@"):
        url = f"https://www.youtube.com/{h}"
    else:
        url = f"https://www.youtube.com/@{h}"
    text = await _get_text(url)
    if not text:
        return None
    m = _CHANNEL_ID_RE.search(text)
    return m.group(1) if m else None


async def fetch_uploads(channel_id: str, attempts: int = 3) -> list[dict[str, Any]] | None:
    """Fetch and parse a channel's recent uploads from its RSS feed.

    Uses browser impersonation (see _get_text) so the feed stops getting
    bot-blocked. Retries with backoff; returns None (not []) when it never
    gets a clean feed, so callers can tell a failure from a genuinely empty
    feed.
    """
    import asyncio

    url = rss_url(channel_id)
    for i in range(attempts):
        text = await _get_text(url)
        if text is not None:
            return parse_uploads_rss(text)
        if i < attempts - 1:
            await asyncio.sleep(1.5 * (i + 1))
    return None


async def fetch_channel_videos(
    channel_id: str, limit: int = 5
) -> list[dict[str, Any]] | None:
    """List a channel's most recent uploads via yt-dlp's InnerTube path, which
    keeps working when YouTube bot-blocks the RSS feed endpoint. Returns
    newest-first [{video_id, title, url, author}] or None on failure. There
    are no upload dates here, so callers dedup by video id, not a watermark."""
    import asyncio

    def _list() -> list[dict[str, Any]] | None:
        try:
            import yt_dlp
        except ImportError:
            return None
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": max(1, limit),
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return None
        entries = (info or {}).get("entries") or []
        out: list[dict[str, Any]] = []
        for e in entries[:limit]:
            vid = e.get("id")
            if not vid:
                continue
            out.append({
                "video_id": vid,
                "title": (e.get("title") or "").strip(),
                "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "author": e.get("uploader") or e.get("channel"),
            })
        return out or None

    return await asyncio.to_thread(_list)


def _chunk_text(c: Any) -> str:
    if isinstance(c, dict):
        return c.get("text", "") or ""
    return getattr(c, "text", "") or ""


def _parse_json3(text: str) -> str | None:
    """Parse YouTube's json3 caption format into plain text."""
    import json

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    parts: list[str] = []
    for ev in data.get("events", []) or []:
        for seg in ev.get("segs", []) or []:
            parts.append(seg.get("utf8", "") or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip() or None


def _parse_vtt(text: str) -> str | None:
    """Parse a WebVTT caption body into plain text (drop cues/timestamps/tags)."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if (not line or line == "WEBVTT" or "-->" in line or line.isdigit()
                or line.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        out.append(re.sub(r"<[^>]+>", "", line))
    return re.sub(r"\s+", " ", " ".join(out)).strip() or None


async def _fetch_transcript_ytdlp(
    video_id: str, languages: tuple[str, ...]
) -> str | None:
    """Primary path: yt-dlp downloads the caption track itself, using the
    real YouTube client context (cookies, signed params) that a bare HTTP
    GET to the timedtext endpoint lacks. We then parse the written file."""
    import asyncio

    try:
        import yt_dlp  # noqa: F401  (presence check; real import is in the thread)
    except ImportError:
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"

    def _download_subs() -> tuple[str, str] | None:
        import glob
        import os
        import shutil
        import tempfile

        import yt_dlp

        from core.transcription import ytdlp_js_opts

        tmp = tempfile.mkdtemp(prefix="atlas_yt_")
        langs = list(languages) + [f"{lang}.*" for lang in languages] + ["en", "en.*"]
        opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(dict.fromkeys(langs)),
            "subtitlesformat": "json3/vtt/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "retries": 3,
            **ytdlp_js_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            for pattern, kind in (("*.json3", "json3"), ("*.vtt", "vtt"),
                                  ("*.srv*", "vtt"), ("*.ttml", "vtt")):
                hits = sorted(glob.glob(os.path.join(tmp, pattern)))
                if hits:
                    with open(hits[0], encoding="utf-8", errors="ignore") as fh:
                        return kind, fh.read()
            return None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    result = await asyncio.to_thread(_download_subs)
    if not result:
        return None
    kind, body = result
    return _parse_json3(body) if kind == "json3" else _parse_vtt(body)


async def _fetch_transcript_api(
    video_id: str, languages: tuple[str, ...]
) -> str | None:
    """Fallback path: youtube-transcript-api, across its 0.6.x / 1.x APIs."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    langs = list(languages)
    chunks: Any = None
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            chunks = list(api.fetch(video_id, languages=langs))
    except Exception:
        chunks = None
    if not chunks:
        try:
            # The 0.x class method; gone in 1.x, where fetch() above is used.
            legacy = getattr(YouTubeTranscriptApi, "get_transcript", None)
            chunks = legacy(video_id, languages=langs) if legacy else None
        except Exception:
            chunks = None
    if not chunks:
        return None
    text = " ".join(_chunk_text(c) for c in chunks)
    return re.sub(r"\s+", " ", text).strip() or None


async def fetch_transcript(
    video_id: str, languages: tuple[str, ...] = ("en",)
) -> str | None:
    """Return a video's plain-text transcript, or None if unavailable.

    Tries yt-dlp first (it impersonates real YouTube clients and is far more
    resilient to rate-limiting than scraping the timedtext endpoint), then
    falls back to youtube-transcript-api. Both deps are imported lazily.
    """
    text = await _fetch_transcript_ytdlp(video_id, languages)
    if text:
        return text
    return await _fetch_transcript_api(video_id, languages)


__all__ = [
    "rss_url", "video_id_from_url", "parse_uploads_rss",
    "resolve_channel_id", "fetch_uploads", "fetch_channel_videos", "fetch_transcript",
]
