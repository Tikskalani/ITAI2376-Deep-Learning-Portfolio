"""Unit tests for the discovery scan's pure helpers.

No DB, no LLM, no network: _new_uploads filters/caps a list of upload
dicts, _parse_vetting pulls a JSON object out of a model reply, _clamp
bounds a score. The live scan (scan_source/run_discovery_scan) is
exercised against real sources from the bot, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.loop.discovery_scan import _clamp, _new_uploads, _parse_vetting


def _u(vid: str, pub: str) -> dict:
    return {"video_id": vid, "title": vid, "published": pub, "url": f"https://yt/{vid}"}


def test_new_uploads_first_run_caps_to_newest():
    ups = [_u(f"v{i}", f"2026-05-{10 + i:02d}T00:00:00+00:00") for i in range(8)]
    out = _new_uploads(ups, None, limit=5)
    assert len(out) == 5
    assert out[0]["video_id"] == "v7"   # newest first
    assert out[-1]["video_id"] == "v3"


def test_new_uploads_filters_by_last_scanned():
    ups = [_u("old", "2026-05-01T00:00:00+00:00"),
           _u("new", "2026-05-20T00:00:00+00:00")]
    last = datetime(2026, 5, 10, tzinfo=UTC)
    out = _new_uploads(ups, last)
    assert [u["video_id"] for u in out] == ["new"]


def test_new_uploads_empty_when_nothing_newer():
    ups = [_u("old", "2026-05-01T00:00:00+00:00")]
    last = datetime(2026, 5, 10, tzinfo=UTC)
    assert _new_uploads(ups, last) == []


def test_parse_vetting_plain_json():
    obj = _parse_vetting(
        '{"verdict":"keep","learning":"x","novelty":0.5,'
        '"relevance":0.8,"credibility":0.6,"rationale":"y"}'
    )
    assert obj["verdict"] == "keep"
    assert obj["relevance"] == 0.8


def test_parse_vetting_with_fence_and_prose():
    raw = 'Sure:\n```json\n{"verdict":"skip","learning":""}\n```\nthanks'
    obj = _parse_vetting(raw)
    assert obj["verdict"] == "skip"


def test_parse_vetting_garbage():
    assert _parse_vetting("no json here") is None
    assert _parse_vetting("") is None


def test_clamp_bounds_and_rejects_garbage():
    assert _clamp(1.5) == 1.0
    assert _clamp(-2) == 0.0
    assert _clamp(0.3) == 0.3
    assert _clamp("not a number") is None
    assert _clamp(None) is None
