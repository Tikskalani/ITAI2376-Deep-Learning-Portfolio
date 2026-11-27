"""Atlas -- output handling for the Windows services.

Under NSSM the stdout and stderr handles Python inherits can block: the
laptop's web service froze after its first line because nobody drained the
stderr pipe. use_log_file() points stdout, stderr (Python and C level) and
faulthandler at a plain append-mode file Atlas owns, which never blocks.
watchdog() then writes every thread's stack to that file if startup has not
finished in time, so a hang names its own line.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path
from typing import TextIO

ROTATE_AT_BYTES = 10 * 1024 * 1024
_stream: TextIO | None = None


def use_log_file(path: str | os.PathLike[str]) -> TextIO:
    """Send all output to `path` (relative to the working directory).

    Rotates once at start when the file is past ROTATE_AT_BYTES, keeping one
    old copy as <name>.1. Never rotates while running: the services hold the
    file open, and Windows will not rename an open file."""
    global _stream
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and p.stat().st_size > ROTATE_AT_BYTES:
            old = p.with_name(p.name + ".1")
            old.unlink(missing_ok=True)
            p.rename(old)
    except OSError:
        pass
    stream = open(p, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - lives for the process
    for fd in (1, 2):
        try:
            os.dup2(stream.fileno(), fd)
        except OSError:
            pass
    sys.stdout = stream
    sys.stderr = stream
    faulthandler.enable(file=stream)
    _stream = stream
    return stream


def watchdog(seconds: float) -> None:
    """Dump every thread's stack to the log file if startup_done() is not
    called within `seconds`. No-op unless use_log_file() ran."""
    if _stream is not None:
        faulthandler.dump_traceback_later(seconds, repeat=False, file=_stream, exit=False)


def startup_done() -> None:
    """Cancel the watchdog. Safe to call whether or not one is armed."""
    faulthandler.cancel_dump_traceback_later()
