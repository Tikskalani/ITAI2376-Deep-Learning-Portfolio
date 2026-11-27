"""Capture: what a shared link is, deduplicated, and its raw material.

Videos (YouTube, Instagram reels and the other yt-dlp site extractors) are
downloaded into data/capabilities/<source>/ through the same guard as
transcription: http(s) on a public host only, site extractors only (no
generic page downloads), at most 720p and 400 MB. Their description and
chapters come with them. A page (an article) is read through core.web's
guarded fetch. Nothing downloaded is ever executed.

What leaves the machine: nothing in this stage; the video is processed
locally (core/capabilities/extract.py says what is sent to the model).
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.config import settings

log = logging.getLogger(__name__)

MAX_BYTES = 400 * 1024 * 1024
MAX_SECONDS = 45 * 60
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|igsh|si$|feature$|ref$|ref_src$)")
_INSTAGRAM = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([\w-]+)")
_TIKTOK = re.compile(r"tiktok\.com/@[\w.-]+/video/(\d+)")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "vimeo.com", "loom.com", "x.com", "twitter.com")


class CaptureError(Exception):
    """The source cannot be captured; the message says why, plainly."""


def canonical(url: str) -> tuple[str, str]:
    """(canonical key, kind) for deduplication. The same video shared from a
    different link (youtu.be, a playlist URL, a tracking parameter) is the
    same source."""
    from core.youtube import video_id_from_url

    url = (url or "").strip()
    vid = video_id_from_url(url)
    if vid and ("youtu" in url):
        return f"youtube:{vid}", "video"
    m = _INSTAGRAM.search(url)
    if m:
        return f"instagram:{m.group(1)}", "video"
    m = _TIKTOK.search(url)
    if m:
        return f"tiktok:{m.group(1)}", "video"
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    kind = "video" if any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS) else "page"
    return urlunparse((p.scheme.lower(), host, p.path.rstrip("/") or "/", "", query, "")), kind


def source_dir(source_id: str) -> Path:
    d = settings.capabilities_dir / str(source_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Video:
    path: Path
    title: str = ""
    author: str = ""
    description: str = ""
    duration: float = 0.0
    chapters: list[dict[str, Any]] = field(default_factory=list)


def _download(url: str, into: Path) -> Video:
    import yt_dlp

    from core.transcription import NO_RUNTIME_HINT, YTDLP_EXTRACTORS, ytdlp_js_opts

    js = ytdlp_js_opts()
    opts: dict[str, Any] = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": str(into / "video.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True, "retries": 2,
        "max_filesize": MAX_BYTES,
        "allowed_extractors": YTDLP_EXTRACTORS,
        "match_filter": lambda info, incomplete=False: (
            f"longer than {MAX_SECONDS // 60} minutes" if (info.get("duration") or 0) > MAX_SECONDS else None),
        **js,
    }
    if settings.ytdlp_cookies_from_browser:
        opts["cookiesfrombrowser"] = (settings.ytdlp_cookies_from_browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many kinds; the message is what he needs
        msg = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
        if not js and ("403" in msg or "format is not available" in msg):
            msg = f"{msg}. {NO_RUNTIME_HINT}"
        raise CaptureError(f"the video could not be downloaded: {msg}") from exc
    files = [f for f in glob.glob(str(into / "video.*")) if os.path.isfile(f) and not f.endswith(".part")]
    if not files:
        raise CaptureError("the video could not be downloaded (nothing was saved; it may be private or too long)")
    return Video(Path(files[0]), info.get("title") or "", info.get("uploader") or info.get("channel") or "",
                 info.get("description") or "", float(info.get("duration") or 0), list(info.get("chapters") or []))


async def video(url: str, source_id: str) -> Video:
    """Download one video for a source. Raises CaptureError."""
    from core.web import FetchRefused, check_public_url

    try:
        await check_public_url(url)
    except FetchRefused as exc:
        raise CaptureError(f"Atlas will not open that link: {exc}") from exc
    return await asyncio.to_thread(_download, url, source_dir(source_id))


async def page(url: str) -> tuple[str, str, str]:
    """(final url, title, readable text) of an article. Raises CaptureError."""
    from core.web import fetch_url

    out = await fetch_url(url)
    if out.get("error"):
        raise CaptureError(f"the page could not be read: {out['error']}")
    return out["url"], out.get("title") or "", out.get("content") or ""


__all__ = ["CaptureError", "Video", "canonical", "page", "source_dir", "video"]
