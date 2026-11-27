"""Atlas -- the worker notices a changed .env.

The Connect tab writes tokens into .env from the dashboard (the web
process). The worker is another process with its own settings object; it
used to need a restart to see a new token. Now it looks at the file's
mtime once a minute and reloads settings in place when it moved, so a
connection made in the browser feeds the next scan.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core import config, envfile
from core.connectors import google_auth

log = logging.getLogger(__name__)

_seen: dict[str, float] = {}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


async def tick(path: Path | None = None) -> bool:
    """True when settings were reloaded. Never raises."""
    p = path or envfile.PATH
    key = str(p)
    now = _mtime(p)
    if now is None:
        return False
    before = _seen.get(key)
    _seen[key] = now
    if before is None or now == before:
        return False
    try:
        config.reload(p)
        google_auth.invalidate()
        log.info("envwatch: .env changed; settings reloaded")
        return True
    except Exception:
        log.exception("envwatch: .env changed but could not be reloaded")
        return False


__all__ = ["tick"]
