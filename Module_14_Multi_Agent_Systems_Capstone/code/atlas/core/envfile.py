"""Atlas -- read and write single keys in the .env file.

Used by the connect wizard and the Google consent script so a token goes
straight from the provider into .env without being pasted by hand (or
echoed to the terminal). Comments, blank lines and every other key are kept
as they are; the file is rewritten in place as UTF-8 without a BOM, the way
deploy.ps1 writes it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.config import REPO_ROOT

PATH = REPO_ROOT / ".env"


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # Unquoted: " #" starts an inline comment, as python-dotenv reads it.
    return re.split(r"(?:^|\s+)#", raw, maxsplit=1)[0].strip()


def read(path: Path | None = None) -> dict[str, str]:
    """Every KEY=value in the file. A missing file is an empty map."""
    p = path or PATH
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, raw = s.split("=", 1)
        out[key.strip()] = _value(raw)
    return out


def get(key: str, path: Path | None = None) -> str:
    return read(path).get(key, "")


def set_value(key: str, value: str, path: Path | None = None) -> None:
    """Set KEY=value: replace the first line for KEY, else append one."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ValueError(f"not an env key: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("a .env value cannot span lines")
    p = path or PATH
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    # Quote when the value would otherwise be cut at a "#" or lose edge spaces.
    out_value = f'"{value}"' if ("#" in value or value != value.strip()) else value
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={out_value}"
            break
    else:
        lines.append(f"{key}={out_value}")
    tmp = p.with_name(p.name + ".tmp")
    # newline="\n" keeps LF on Windows too, so an edit never mixes line endings.
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, p)


__all__ = ["PATH", "get", "read", "set_value"]
