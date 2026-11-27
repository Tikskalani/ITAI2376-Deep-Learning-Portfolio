"""yt-dlp's JavaScript runtime (2026-10-24): YouTube answered 403 to the first
real reel until yt-dlp could solve its player challenge. Deno is the only
runtime Atlas names, because yt-dlp runs it without permissions; Node would
run YouTube's script with full access to the laptop."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from core import transcription
from core.capabilities import capture


def test_deno_on_path_is_the_only_runtime(monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda name: {"deno": "C:/deno/deno.exe",
                                                                      "node": "C:/node/node.exe"}.get(name))
    assert transcription.ytdlp_js_opts() == {"js_runtimes": {"deno": {"path": "C:/deno/deno.exe"}}}


def test_deno_found_where_the_installer_puts_it(monkeypatch, tmp_path):
    exe = tmp_path / ".deno" / "bin" / "deno.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcription.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert transcription.deno_path() == str(exe)


def test_no_deno_means_no_runtime_never_node(monkeypatch, tmp_path):
    monkeypatch.setattr(transcription.shutil, "which", lambda name: "C:/node/node.exe" if name == "node" else None)
    monkeypatch.setattr(transcription.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert transcription.ytdlp_js_opts() == {}


class _Refusing:
    def __init__(self, opts):
        self.opts = opts
        _Refusing.seen = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        raise RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden")


@pytest.mark.parametrize("deno", [None, "C:/deno/deno.exe"])
def test_capture_names_deno_and_explains_a_403(monkeypatch, tmp_path, deno):
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_Refusing))
    monkeypatch.setattr(transcription, "deno_path", lambda: deno)
    with pytest.raises(capture.CaptureError) as err:
        capture._download("https://www.youtube.com/shorts/x", Path(tmp_path))
    if deno:
        assert _Refusing.seen["js_runtimes"] == {"deno": {"path": deno}}
        assert "winget" not in str(err.value)
    else:
        assert "js_runtimes" not in _Refusing.seen
        assert "winget install DenoLand.Deno" in str(err.value)
