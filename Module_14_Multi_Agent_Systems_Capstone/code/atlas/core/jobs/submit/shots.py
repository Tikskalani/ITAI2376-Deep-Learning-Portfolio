"""Screenshots a person can read.

A full-page capture of a careers page is one image 1280 wide and 6000 tall:
shown on a phone or in a card it is a narrow strip (2026-10-23). The
browser captures the application form only (core/jobs/submit/browser.py),
and tile() cuts anything taller than a screen into numbered pages a screen
high, with a small overlap so no field is cut in half unseen.
"""

from __future__ import annotations

from pathlib import Path

PAGE_HEIGHT = 1000   # px: about one laptop screen of form
OVERLAP = 60         # px repeated at the top of each next page
SLACK = 1.3          # an image up to 1.3 pages tall stays whole
MIN_TAIL = 200       # px: a shorter remainder joins the page above it


def tile(path: Path, page_height: int = PAGE_HEIGHT, overlap: int = OVERLAP) -> list[Path]:
    """Cut the image at path into <stem>-1.png, <stem>-2.png... and remove the
    original. Returns the pages in order; a short image is returned as is."""
    from PIL import Image

    with Image.open(path) as img:
        width, height = img.size
        if height <= page_height * SLACK:
            return [path]
        pages: list[Path] = []
        top, n = 0, 1
        while top < height:
            bottom = min(top + page_height, height)
            # A short tail joins this page rather than becoming a sliver page.
            if height - bottom < MIN_TAIL:
                bottom = height
            out = path.with_name(f"{path.stem}-{n}{path.suffix}")
            img.crop((0, top, width, bottom)).save(out)
            pages.append(out)
            if bottom >= height:
                break
            top, n = bottom - overlap, n + 1
    path.unlink()
    return pages


__all__ = ["PAGE_HEIGHT", "tile"]
