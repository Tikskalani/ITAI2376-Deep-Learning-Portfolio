"""The connect wizard: .env editing and the live token checks.

No network: every provider call goes through an httpx MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from core import envfile
from scripts import connect

# ------------------------------------------------------------------ envfile


def test_read_handles_comments_quotes_and_inline_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# header\n"
        "\n"
        "A=1\n"
        "ATLAS_GITHUB_REPOS=                    # e.g. owner/repo\n"
        'B="has # hash"\n'
        "C='single'\n"
        "D = spaced \n",
        encoding="utf-8",
    )
    env = envfile.read(p)
    assert env == {"A": "1", "ATLAS_GITHUB_REPOS": "", "B": "has # hash", "C": "single", "D": "spaced"}


def test_read_missing_file_is_empty(tmp_path):
    assert envfile.read(tmp_path / "nope") == {}


def test_set_value_replaces_in_place_and_keeps_everything_else(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# keep me\nATLAS_CANVAS_TOKEN=\nOTHER=x\n", encoding="utf-8")
    envfile.set_value("ATLAS_CANVAS_TOKEN", "abc~123", p)
    assert p.read_text(encoding="utf-8") == "# keep me\nATLAS_CANVAS_TOKEN=abc~123\nOTHER=x\n"
    envfile.set_value("NEW_KEY", "v", p)
    assert p.read_text(encoding="utf-8").endswith("OTHER=x\nNEW_KEY=v\n")


def test_set_value_quotes_hash_and_round_trips(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    envfile.set_value("SECRET", "a#b", p)
    assert envfile.get("SECRET", p) == "a#b"


def test_set_value_strips_bom_and_creates_file(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes("﻿A=1\n".encode())
    envfile.set_value("A", "2", p)
    assert p.read_bytes() == b"A=2\n"
    q = tmp_path / "fresh.env"
    envfile.set_value("X", "1", q)
    assert q.read_text() == "X=1\n"


@pytest.mark.parametrize("key,value", [("lower", "x"), ("A B", "x"), ("OK", "two\nlines")])
def test_set_value_rejects_bad_input(tmp_path, key, value):
    with pytest.raises(ValueError):
        envfile.set_value(key, value, tmp_path / ".env")


# ------------------------------------------------------------------ checks


def _client(routes: dict[tuple[str, str], tuple[int, object]], seen: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        key = (request.method, f"{request.url.scheme}://{request.url.host}{request.url.path}")
        status, body = routes.get(key, (404, {"message": "no route"}))
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_canvas_not_configured():
    assert connect.check_canvas("", "").ok is None


def test_canvas_ok_sends_bearer():
    seen: list[httpx.Request] = []
    client = _client(
        {("GET", "https://hccs.instructure.com/api/v1/users/self"): (200, {"name": "Trilok Kalani"})}, seen
    )
    r = connect.check_canvas("https://hccs.instructure.com/", "tok", client)
    assert r.ok is True and "Trilok Kalani" in r.detail
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_canvas_refused():
    client = _client({("GET", "https://hccs.instructure.com/api/v1/users/self"): (401, {})})
    r = connect.check_canvas("https://hccs.instructure.com", "bad", client)
    assert r.ok is False and "refused" in r.detail


def test_canvas_unreachable():
    def boom(request):
        raise httpx.ConnectError("dns")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    r = connect.check_canvas("https://nowhere.example", "t", client)
    assert r.ok is False and "could not reach" in r.detail


def test_github_ok_and_refused():
    ok = _client({("GET", "https://api.github.com/user"): (200, {"login": "atlas-dev"})})
    assert connect.check_github("t", ok).detail == "signed in as atlas-dev"
    bad = _client({("GET", "https://api.github.com/user"): (401, {})})
    assert connect.check_github("t", bad).ok is False
    assert connect.check_github("").ok is None


def test_github_repos_lists_names_in_order():
    client = _client(
        {("GET", "https://api.github.com/user/repos"): (200, [{"full_name": "a/x"}, {"full_name": "a/y"}, {}])}
    )
    assert connect.github_repos("t", client) == ["a/x", "a/y"]


def test_github_repos_empty_on_error():
    client = _client({("GET", "https://api.github.com/user/repos"): (403, {})})
    assert connect.github_repos("t", client) == []


GOOGLE_OK = {
    ("POST", "https://oauth2.googleapis.com/token"): (200, {"access_token": "at", "expires_in": 3600}),
    ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile"): (200, {"emailAddress": "me@x.com"}),
    ("GET", "https://www.googleapis.com/calendar/v3/calendars/primary"): (200, {"summary": "me@x.com"}),
}


def test_google_not_configured_names_what_is_missing():
    assert "client" in connect.check_google("", "", "").detail
    assert "consent" in connect.check_google("id", "secret", "").detail


def test_google_ok():
    seen: list[httpx.Request] = []
    r = connect.check_google("id", "secret", "rt", _client(GOOGLE_OK, seen))
    assert r.ok is True
    assert r.detail == "Gmail me@x.com, Calendar me@x.com"
    assert seen[1].headers["authorization"] == "Bearer at"


def test_google_invalid_grant_says_consent_again():
    routes = dict(GOOGLE_OK)
    routes[("POST", "https://oauth2.googleapis.com/token")] = (400, {"error": "invalid_grant"})
    r = connect.check_google("id", "secret", "rt", _client(routes))
    assert r.ok is False and "consent expired" in r.detail


def test_google_invalid_client():
    routes = dict(GOOGLE_OK)
    routes[("POST", "https://oauth2.googleapis.com/token")] = (401, {"error": "invalid_client"})
    assert "client id or secret" in connect.check_google("id", "s", "rt", _client(routes)).detail


def test_google_api_not_enabled_hints():
    routes = dict(GOOGLE_OK)
    routes[("GET", "https://www.googleapis.com/calendar/v3/calendars/primary")] = (403, {})
    r = connect.check_google("id", "s", "rt", _client(routes))
    assert r.ok is False and "Calendar API enabled" in r.detail
    routes[("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile")] = (403, {})
    assert "Gmail API enabled" in connect.check_google("id", "s", "rt", _client(routes)).detail


# ------------------------------------------------------------------ main


def _no_network(monkeypatch, results: dict[str, connect.Result]):
    monkeypatch.setattr(connect, "check_all", lambda env: results)


def test_check_mode_exit_codes(monkeypatch, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    monkeypatch.setattr(envfile, "PATH", env)
    _no_network(monkeypatch, {
        "google": connect.Result("Google", None, "not connected"),
        "canvas": connect.Result("Canvas", True, "signed in as T"),
        "github": connect.Result("GitHub", None, "not connected"),
    })
    assert connect.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "ok  Canvas" in out and "--  Google" in out
    _no_network(monkeypatch, {
        "google": connect.Result("Google", False, "consent expired"),
        "canvas": connect.Result("Canvas", True, "ok"),
        "github": connect.Result("GitHub", True, "ok"),
    })
    assert connect.main(["--check"]) == 1


def test_main_without_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(envfile, "PATH", tmp_path / "missing.env")
    assert connect.main(["--check"]) == 2


def test_main_runs_only_what_is_missing_and_reports_change(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    monkeypatch.setattr(envfile, "PATH", env)
    _no_network(monkeypatch, {
        "google": connect.Result("Google", True, "ok"),
        "canvas": connect.Result("Canvas", None, "not connected"),
        "github": connect.Result("GitHub", True, "ok"),
        "alarm": connect.Result("Alarm", True, "ok"),
    })
    ran: list[str] = []
    monkeypatch.setattr(connect, "SETUP", {
        name: (lambda n: (lambda e: ran.append(n) or True))(name) for name in connect.SOURCES
    })
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert connect.main([]) == connect.CHANGED
    assert ran == ["canvas"]
    ran.clear()
    assert connect.main(["--redo", "github"]) == connect.CHANGED
    assert ran == ["canvas", "github"]


def test_main_nothing_to_do(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    monkeypatch.setattr(envfile, "PATH", env)
    _no_network(monkeypatch, {k: connect.Result(k, True, "ok") for k in connect.SOURCES})
    assert connect.main([]) == 0


def test_bad_source_name_is_rejected():
    with pytest.raises(SystemExit):
        connect.main(["--redo", "slack"])


def test_alarm_checks():
    assert connect.check_alarm("").ok is None
    assert connect.check_alarm("http://hc-ping.com/x").ok is False
    ok = _client({("GET", "https://hc-ping.com/abc"): (200, "OK")})
    r = connect.check_alarm("https://hc-ping.com/abc", ok)
    assert r.ok is True and "hc-ping.com" in r.detail
    bad = _client({("GET", "https://hc-ping.com/abc"): (404, "not found")})
    assert connect.check_alarm("https://hc-ping.com/abc", bad).ok is False
