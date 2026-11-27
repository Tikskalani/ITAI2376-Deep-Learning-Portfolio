"""Readable form screenshots (core/jobs/submit/shots.py): a tall capture is cut
into pages a screen high; a short one stays whole. 2026-10-23: a full-page
capture was 1280 x 5962, a narrow strip on any screen."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from core.jobs.submit import shots


def _image(path: Path, w: int, h: int) -> Path:
    Image.new("RGB", (w, h), "white").save(path)
    return path


def test_a_tall_capture_becomes_screen_high_pages(tmp_path):
    pages = shots.tile(_image(tmp_path / "filled.png", 640, 2500), page_height=1000, overlap=60)
    assert [p.name for p in pages] == ["filled-1.png", "filled-2.png", "filled-3.png"]
    sizes = [Image.open(p).size for p in pages]
    assert sizes[0] == (640, 1000) and sizes[1] == (640, 1000) and sizes[2][1] == 2500 - 2 * 940
    assert not (tmp_path / "filled.png").exists(), "the strip is replaced by its pages"


def test_a_short_capture_stays_whole(tmp_path):
    path = _image(tmp_path / "filled.png", 640, 1200)
    assert shots.tile(path, page_height=1000) == [path] and path.exists()


def test_no_page_is_only_overlap(tmp_path):
    # 1000 + 940 = 1940: a 1990 px image would leave a 50 px sliver past the overlap.
    pages = shots.tile(_image(tmp_path / "f.png", 300, 1990), page_height=1000, overlap=60)
    assert len(pages) == 2 and Image.open(pages[-1]).size[1] == 1990 - 940
