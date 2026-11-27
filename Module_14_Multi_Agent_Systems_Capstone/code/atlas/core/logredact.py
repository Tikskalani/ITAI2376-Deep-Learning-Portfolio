"""Atlas -- secrets never reach a log line.

One logging.Filter, attached to every handler of the root and uvicorn
loggers, rewrites each record's message before a handler writes it: Google
client secrets (GOCSPX-...), refresh tokens (1//...), access tokens
(ya29....), authorization codes (4/...), the form fields and URL parameters
that carry them, bearer headers, GitHub and Anthropic keys. Code that logs a
secret is still a bug (core/connect.py logs none); this is the net under it.
"""

from __future__ import annotations

import logging
import re

# Carriers first (a header, a form field), then the token shapes themselves,
# so a bearer value is replaced once and not matched again in pieces.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=\-]+"), r"\1***"),
    (re.compile(r"\b((?:code|client_secret|refresh_token|access_token|token)=)[^&\s\"'<>]+"), r"\1***"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"), "GOCSPX-***"),
    (re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_\-]{8,}"), "1//***"),
    (re.compile(r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_\-.]+"), "ya29.***"),
    (re.compile(r"(?<![A-Za-z0-9])4/[A-Za-z0-9_\-]{8,}"), "4/***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "gh*_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"), "github_pat_***"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
]

_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def redact(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactSecrets(logging.Filter):
    """Rewrites the record in place; never drops it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is the formatter's problem, not ours
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install() -> None:
    """Attach the filter to every handler the root and uvicorn loggers have
    right now. Idempotent; call it again after a logger gains handlers."""
    for name in _LOGGERS:
        for handler in logging.getLogger(name or None).handlers:
            if not any(isinstance(f, RedactSecrets) for f in handler.filters):
                handler.addFilter(RedactSecrets())


__all__ = ["RedactSecrets", "install", "redact"]
