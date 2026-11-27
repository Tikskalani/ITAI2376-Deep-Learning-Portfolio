"""core/logredact.py: a secret in a log call never reaches the handler's output."""

from __future__ import annotations

import io
import logging

from core import logredact


def test_redact_covers_every_secret_shape_and_leaves_ordinary_text_alone():
    cases = {
        "secret GOCSPX-abcDEF_123-xyz here": "secret GOCSPX-*** here",
        "refresh 1//0gabcdefghijklmnop-ABC done": "refresh 1//*** done",
        "access ya29.a0AfH6SMBxyz-_.q": "access ya29.***",
        "code 4/0AX4XfWhabcdefgh": "code 4/***",
        "GET /cb?code=4%2F0AX4XfWh&state=abc": "GET /cb?code=***&state=abc",
        "client_secret=GOCSPX-x&grant_type=authorization_code": "client_secret=***&grant_type=authorization_code",
        "refresh_token=1//abc&x=1": "refresh_token=***&x=1",
        "Authorization: Bearer ya29.abc.def": "Authorization: Bearer ***",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123": "token gh*_***",
        "pat github_pat_11AAAAAAA0abcdefghijklmnop": "pat github_pat_***",
        "key sk-ant-api03-abcdefghijk": "key sk-ant-***",
        "status_code=200 took 14/20 steps, state=abc": "status_code=200 took 14/20 steps, state=abc",
    }
    for raw, clean in cases.items():
        assert logredact.redact(raw) == clean, raw


def test_the_filter_scrubs_what_a_handler_writes_and_installs_once():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logredact.install()
        logredact.install()
        assert sum(isinstance(f, logredact.RedactSecrets) for f in handler.filters) == 1
        logging.getLogger("test.redact").warning("exchange failed for %s with %s", "GOCSPX-leaky_fake", "1//0gLeakyRefresh_token")
        logging.getLogger("test.redact").warning("plain line %d", 7)
    finally:
        root.removeHandler(handler)
    out = stream.getvalue()
    assert "leaky" not in out and "Leaky" not in out
    assert "exchange failed for GOCSPX-*** with 1//***" in out and "plain line 7" in out
