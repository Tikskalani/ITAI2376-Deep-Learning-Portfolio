"""core.service_io: the services write their own log, and a hang names its line."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from core.config import REPO_ROOT


def _run(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )


def test_output_goes_to_the_file_not_the_console(tmp_path):
    log = tmp_path / "logs" / "web.log"
    proc = _run(f"""
        import logging, sys
        from core.service_io import use_log_file
        use_log_file(r"{log}")
        print("hello from print")
        sys.stderr.write("hello from stderr\\n")
        logging.basicConfig(level=logging.INFO, force=True)
        logging.getLogger("t").info("hello from logging")
    """, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""
    text = log.read_text(encoding="utf-8")
    for line in ("hello from print", "hello from stderr", "hello from logging"):
        assert line in text


def test_watchdog_dumps_stacks_when_startup_hangs(tmp_path):
    log = tmp_path / "web.log"
    proc = _run(f"""
        import time
        from core.service_io import use_log_file, watchdog
        use_log_file(r"{log}")
        watchdog(0.3)
        def stuck_in_startup():
            time.sleep(1.0)
        stuck_in_startup()
    """, tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = log.read_text(encoding="utf-8")
    assert "stuck_in_startup" in text


def test_watchdog_stays_quiet_once_startup_is_done(tmp_path):
    log = tmp_path / "web.log"
    proc = _run(f"""
        import time
        from core.service_io import startup_done, use_log_file, watchdog
        use_log_file(r"{log}")
        watchdog(0.3)
        startup_done()
        time.sleep(0.6)
    """, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Thread" not in log.read_text(encoding="utf-8")


def test_rotates_once_at_start_when_large(tmp_path):
    log = tmp_path / "web.log"
    log.write_text("x" * 64, encoding="utf-8")
    proc = _run(f"""
        import core.service_io as s
        s.ROTATE_AT_BYTES = 10
        s.use_log_file(r"{log}")
        print("fresh")
    """, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "web.log.1").read_text(encoding="utf-8") == "x" * 64
    assert log.read_text(encoding="utf-8").strip() == "fresh"
