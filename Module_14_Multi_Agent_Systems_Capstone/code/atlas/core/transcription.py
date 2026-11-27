"""Atlas -- media transcription (platform-agnostic).

Downloads a video or reel's audio with yt-dlp (which impersonates a real
client via curl_cffi and can use the browser's login cookies), then
transcribes it locally with faster-whisper. No API key, no third party, no
per-call cost.

This is the robust path for two reasons:
  * Instagram has no transcript endpoint at all -- audio is the only way.
  * For YouTube it routes around the rate-limited timedtext caption
    endpoint by transcribing the audio directly.

get_transcript(url) is the unified entry: for a YouTube URL it tries the
fast caption path first and falls back to audio transcription; for anything
else (Instagram, etc.) it goes straight to audio transcription.

The URL can come from a model (the fetch_transcript tool), so it is held to
the same rule as fetch_url: http(s) on a public host only. yt-dlp follows
its own redirects, which that first-hop check cannot see, so yt-dlp runs
without its "generic" extractor: only site extractors (YouTube, Instagram,
...) that talk to their own services, never a downloader that GETs any page
it is handed, such as Atlas's API on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()

# Every site extractor, minus the one that downloads arbitrary pages.
YTDLP_EXTRACTORS = ["default", "-generic"]


def deno_path() -> str | None:
    """Where Deno is, if installed. YouTube answers 403 (or offers no formats)
    unless yt-dlp solves its player challenge in a JavaScript runtime
    (2026-10-24). Deno only: yt-dlp runs it with no file, network or env
    permissions, so YouTube's script cannot touch the laptop. Node would run
    the same script with full access, so Atlas never enables it. The services
    may not see the user's PATH, so the usual install folders are tried too."""
    found = shutil.which("deno")
    if found:
        return found
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    for candidate in (home / ".deno" / "bin" / "deno.exe", local / "Microsoft" / "WinGet" / "Links" / "deno.exe",
                      home / ".deno" / "bin" / "deno"):
        if candidate.is_file():
            return str(candidate)
    return None


def ytdlp_js_opts() -> dict[str, Any]:
    """The yt-dlp options that name Deno as the only JavaScript runtime, or
    nothing when it is not installed (yt-dlp then tries without one)."""
    path = deno_path()
    return {"js_runtimes": {"deno": {"path": path}}} if path else {}


NO_RUNTIME_HINT = ("YouTube now needs Deno to hand out its videos; install it once with "
                   "'winget install DenoLand.Deno' and press Try again")


def _get_model():
    """Lazy, thread-safe faster-whisper model singleton. Returns None if the
    library or model cannot be loaded (so callers degrade, not crash)."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                log.warning("faster-whisper not installed; cannot transcribe audio")
                return None
            try:
                _model = WhisperModel(
                    settings.whisper_model, device="cpu", compute_type="int8"
                )
                log.info("loaded faster-whisper model %r", settings.whisper_model)
            except Exception:
                log.exception("could not load faster-whisper model %r",
                              settings.whisper_model)
                return None
    return _model


def _download_audio(url: str, into_dir: str) -> str | None:
    """Download just the audio of one video/reel into into_dir. Returns the
    file path, or None on failure. Uses browser cookies if configured."""
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(into_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "allowed_extractors": YTDLP_EXTRACTORS,
        **ytdlp_js_opts(),
    }
    cookies = settings.ytdlp_cookies_from_browser
    if cookies:
        # e.g. "chrome" -> yt-dlp reads the logged-in Instagram/YouTube session
        opts["cookiesfrombrowser"] = (cookies,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        log.exception("audio download failed for %s", url)
        return None
    files = [f for f in glob.glob(os.path.join(into_dir, "*")) if os.path.isfile(f)]
    return files[0] if files else None


def _transcribe_file(path: str) -> str | None:
    """Transcribe a local audio file with faster-whisper. faster-whisper
    decodes the container itself (bundled libs), so no system ffmpeg needed."""
    model = _get_model()
    if model is None:
        return None
    try:
        # task="translate" makes Whisper output English regardless of the
        # spoken language, so Hindi (and any other) reels are understood.
        segments, _info = model.transcribe(path, beam_size=1, task="translate")
        text = " ".join(seg.text for seg in segments)
    except Exception:
        log.exception("whisper transcription failed for %s", path)
        return None
    return re.sub(r"\s+", " ", text).strip() or None


async def transcribe_url(url: str) -> str | None:
    """Download a video/reel's audio and transcribe it. Returns text or None.
    Runs the blocking download + inference in a worker thread. A URL that is
    not http(s) on a public host returns None without downloading."""
    from core.web import FetchRefused, check_public_url

    try:
        await check_public_url(url)
    except FetchRefused as exc:
        log.warning("audio download refused: %s", exc)
        return None

    def _work() -> str | None:
        tmp = tempfile.mkdtemp(prefix="atlas_aud_")
        try:
            path = _download_audio(url, tmp)
            if not path:
                return None
            return _transcribe_file(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return await asyncio.to_thread(_work)


async def get_transcript(url: str) -> str | None:
    """Unified transcript entry. YouTube: fast caption path first, then audio
    transcription. Anything else (Instagram, etc.): audio transcription.
    A URL that is not http(s) on a public host returns None."""
    from core.web import FetchRefused, check_public_url
    from core.youtube import fetch_transcript, video_id_from_url

    try:
        await check_public_url(url)
    except FetchRefused as exc:
        log.warning("transcript refused: %s", exc)
        return None

    vid = video_id_from_url(url)
    if vid:
        captions = await fetch_transcript(vid)
        if captions:
            return captions
        log.info("captions unavailable for %s; falling back to audio transcription", vid)
    return await transcribe_url(url)


__all__ = ["transcribe_url", "get_transcript"]
