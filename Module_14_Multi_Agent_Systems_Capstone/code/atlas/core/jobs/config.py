"""config/jobs.yaml, read once and re-read when it changes."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from core.config import REPO_ROOT

log = logging.getLogger(__name__)

PATH = REPO_ROOT / "config" / "jobs.yaml"
# The master resume every tailored version is built from. Gitignored (data/).
RESUME_DIR = REPO_ROOT / "data" / "resume"

_cache: dict[str, Any] = {"mtime": None, "data": {}}


def load(path: Path | None = None) -> dict[str, Any]:
    p = path or PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if path is None and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
    except Exception as exc:  # noqa: BLE001 - a bad edit must not take Atlas down
        log.warning("jobs config %s unreadable: %s", p, exc)
        return {}
    if path is None:
        _cache.update(mtime=mtime, data=data)
    return data


def profile(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return (load() if data is None else data).get("profile") or {}


def companies(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [c for c in (load() if data is None else data).get("companies") or [] if c.get("board") and c.get("source")]


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    parts = []
    for page in PdfReader(str(path)).pages:
        try:
            parts.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 - one unreadable page is skipped
            continue
    return "\n\n".join(x for x in parts if x)


def _docx_text(path: Path) -> str:
    """Paragraph text of a .docx (it is a zip of XML; no extra dependency).

    A tab inside a line (the usual way a resume pushes dates to the right
    margin) becomes " | ", so "Company<tab>2026 to present" reads as
    "Company | 2026 to present" instead of running together."""
    import zipfile
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(f"{ns}p"):
        parts: list[str] = []
        for el in p.iter():
            if el.tag == f"{ns}t":
                parts.append(el.text or "")
            elif el.tag in (f"{ns}tab", f"{ns}ptab") and parts and "".join(parts).strip():
                parts.append(" | ")
        line = re.sub(r"(\s*\|\s*)+$", "", "".join(parts))
        line = re.sub(r"(\s*\|\s*){2,}", " | ", line)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def resume_text(directory: Path | None = None) -> str:
    """The newest resume in data/resume/ (.md, .txt, .pdf or .docx) as text, or ''."""
    d = directory or RESUME_DIR
    try:
        files = sorted(
            (f for f in d.iterdir() if f.suffix.lower() in (".md", ".txt", ".pdf", ".docx")),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""
    for f in files:
        try:
            ext = f.suffix.lower()
            if ext == ".pdf":
                text = _pdf_text(f)
            elif ext == ".docx":
                text = _docx_text(f)
            else:
                text = f.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("resume %s unreadable: %s", f.name, exc)
            continue
        if text.strip():
            return text.strip()
    return ""


__all__ = ["PATH", "RESUME_DIR", "companies", "load", "profile", "resume_text"]
