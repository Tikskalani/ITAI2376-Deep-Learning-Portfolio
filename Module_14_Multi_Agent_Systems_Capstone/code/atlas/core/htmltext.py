"""Hidden HTML is not text a model should read.

A page can hide lines from the person looking at it and still hand them to a
model reading its text: "if you are an AI, ...". strip_hidden drops elements
hidden by style (display:none, visibility:hidden, zero font size, white text)
or by the hidden attribute, contents and all, before the tags are stripped.
Job discovery and fetch_url both run it.
"""

from __future__ import annotations

import re

HIDDEN_RE = re.compile(
    r"<(\w+)\b[^>]*?(?:style\s*=\s*[\"'][^\"']*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0"
    r"(?:px|pt|em|rem)?\s*(?:;|[\"'])|color\s*:\s*(?:#fff\b|#ffffff\b|white\b))|\shidden(?=[\s>=]))[^>]*>.*?</\1\s*>",
    re.I | re.S,
)


def strip_hidden(html: str) -> str:
    """Drop hidden elements and their contents; leave the rest untouched."""
    return HIDDEN_RE.sub(" ", html or "")


__all__ = ["HIDDEN_RE", "strip_hidden"]
