"""The Connect tab (core/connect.py): one tap per app, against a private .env and
fake providers. The real settings object is never reloaded here, and no test
value here is a real credential."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from core import connect, envfile
from core.connectors import google_auth, state
from core.loop import envwatch
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"X-Atlas-Client": "1", "Origin": "http://127.0.0.1:8000"}
CLIENT_ID = "123.apps.googleusercontent.com"
SECRET = "GOCSPX-fake_test"     # the shape Google issues; not a real one
CB = "http://127.0.0.1:8000/api/connect/google/callback"
REVOKED: list[str] = []


def _provider(request: httpx.Request) -> httpx.Response:
    """Google, GitHub, Canvas and healthchecks, as far as the checks look."""
    url, path = str(request.url), request.url.path
    auth = request.headers.get("authorization", "")
    if url.startswith("https://oauth2.googleapis.com/token"):
        form = parse_qs(request.content.decode())
        if form.get("client_secret") != [SECRET]:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "The provided client secret is invalid."})
        if form.get("grant_type") == ["authorization_code"]:
            if ":9999/" in form.get("redirect_uri", [""])[0]:
                return httpx.Response(400, json={"error": "redirect_uri_mismatch", "error_description": "Bad Request"})
            code = form.get("code", [""])[0]
            if code == "good-code":
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt-1", "scope": "x"})
            if code == "no-refresh-code":
                return httpx.Response(200, json={"access_token": "at", "scope": "x"})
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
        if form.get("refresh_token") == ["rt-1"]:
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(400, json={"error": "invalid_grant"})
    if url.startswith("https://oauth2.googleapis.com/revoke"):
        REVOKED.append(parse_qs(request.content.decode()).get("token", [""])[0])
        return httpx.Response(200, json={})
    if path.endswith("/gmail/v1/users/me/profile"):
        return httpx.Response(200, json={"emailAddress": "person3@gmail.com"})
    if path.endswith("/calendars/primary"):
        return httpx.Response(200, json={"summary": "Trilok"})
    if request.url.host == "api.github.com":
        if auth != "Bearer gh-good":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(200, json={"login": "atlas-dev"})
        if path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "atlas-dev/atlas"}, {"full_name": "atlas-dev/sidework"}])
    if path.endswith("/api/v1/users/self"):
        return httpx.Response(200 if auth == "Bearer cv-good" else 401, json={"name": "Trilok Kalani"})
    if request.url.host == "hc-ping.com":
        return httpx.Response(200, text="OK")
    return httpx.Response(404, text="nope")


def _mcp(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    if request.headers.get("authorization", "") not in ("", "Bearer z-good"):
        return httpx.Response(401, text="no")
    if request.url.host == "broken.example":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "not here"}})
    if body.get("method") == "initialize":
        sse = ('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",'
               '"serverInfo":{"name":"Zapier MCP","version":"1.2"},"capabilities":{}}}\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
    if body.get("method") == "notifications/initialized":
        assert request.headers.get("mcp-session-id") == "s1"
        return httpx.Response(202)
    if body.get("method") == "tools/list":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [
            {"name": "gmail_find_email"}, {"name": "google_calendar_create_event"}, {"name": "notion_search"}]}})
    return httpx.Response(400)


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("ATLAS_TIMEZONE=America/Chicago\n", encoding="utf-8")
    reloads: list[int] = []
    monkeypatch.setattr(envfile, "PATH", env)
    # mcp_servers() falls back to the settings loaded from the real .env at
    # startup; a laptop with Zapier connected must not leak into the test.
    monkeypatch.setattr(connect.settings, "mcp_servers_json", "")
    monkeypatch.setattr(connect, "_reload", lambda: reloads.append(1))
    monkeypatch.setattr(connect, "CLIENT_FACTORY", lambda: httpx.Client(transport=httpx.MockTransport(_provider)))
    monkeypatch.setattr(connect, "ASYNC_TRANSPORT", httpx.MockTransport(_mcp))
    connect._pending.clear()
    REVOKED.clear()
    google_auth.invalidate()
    async with livedb.shadow(monkeypatch, "connector_state"):
        yield type("W", (), {"env": env, "reloads": reloads})
    google_auth.invalidate()


def _state_for(url: str = CB) -> str:
    return parse_qs(urlparse(connect.google_auth_url(url)).query)["state"][0]


@aio
async def test_nothing_connected_then_one_paste_each(world):
    s = await connect.status()
    assert s["checked"] and s["google"] == {**s["google"], "ok": None, "client": False, "consented": False, "needs_reconnect": False}
    assert s["github"]["ok"] is None and s["canvas"]["ok"] is None and s["alarm"]["ok"] is None and s["mcp"]["servers"] == []

    gh = await connect.set_github("gh-good")
    assert gh["ok"] and gh["detail"] == "signed in as atlas-dev" and gh["repos"] == ["atlas-dev/atlas", "atlas-dev/sidework"]
    assert envfile.get("ATLAS_GITHUB_TOKEN", world.env) == "gh-good" and world.reloads
    with pytest.raises(ValueError, match="refused"):
        await connect.set_github("gh-bad")
    assert envfile.get("ATLAS_GITHUB_TOKEN", world.env) == "gh-good"          # a bad paste changes nothing

    cv = await connect.set_canvas("", "cv-good")
    assert cv["ok"] and cv["url"] == "https://hccs.instructure.com" and envfile.get("ATLAS_CANVAS_URL", world.env) == cv["url"]
    with pytest.raises(ValueError):
        await connect.set_canvas("http://insecure", "cv-good")
    with pytest.raises(ValueError):
        await connect.set_canvas("https://hccs.instructure.com", "cv-bad")

    al = await connect.set_alarm("https://hc-ping.com/abc")
    assert al["ok"] and "hc-ping.com" in al["detail"]
    with pytest.raises(ValueError):
        await connect.set_alarm("http://hc-ping.com/abc")

    s = await connect.status()
    assert s["github"]["ok"] and s["canvas"]["ok"] and s["alarm"]["ok"] and s["github"]["configured"]
    s2 = await connect.status(check=False)
    assert s2["checked"] is False and s2["github"]["detail"] == "configured (not checked)"


def test_the_oauth_client_is_checked_before_it_is_saved(world):
    for bad, why in [("", "paste the client secret"), ("GOCSPX-abc def", "space or line break"),
                     ("GOCSPX-****oLlJ", "masked copy"), ("GOCSPX-••••", "masked copy"),
                     (CLIENT_ID, "client id pasted as the secret"), ("sec", "starts with GOCSPX-")]:
        with pytest.raises(ValueError, match=why):
            connect.google_set_client(CLIENT_ID, bad)
    with pytest.raises(ValueError, match="apps.googleusercontent.com"):
        connect.google_set_client("not-a-client", SECRET)
    assert envfile.get("ATLAS_GOOGLE_CLIENT_SECRET", world.env) == ""          # a bad paste wrote nothing
    out = connect.google_set_client(f'  "{CLIENT_ID}" ', f"'{SECRET}'\n")       # quotes and edges come off
    assert out == {"client": True, "client_id_prefix": "123.apps.goo", "consented": False, "tokens_cleared": False}
    assert envfile.get("ATLAS_GOOGLE_CLIENT_ID", world.env) == CLIENT_ID
    assert envfile.get("ATLAS_GOOGLE_CLIENT_SECRET", world.env) == SECRET and world.reloads


@aio
async def test_google_is_one_tap_from_the_laptop(world, caplog):
    caplog.set_level(logging.INFO, logger="core.connect")
    with pytest.raises(ValueError):
        connect.google_auth_url(CB)                                              # no client yet
    connect.google_set_client(CLIENT_ID, SECRET)
    with pytest.raises(ValueError, match="laptop"):
        connect.google_auth_url("http://atlas.tail1234.ts.net/api/connect/google/callback")
    url = connect.google_auth_url(CB)
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "accounts.google.com" and q["client_id"] == [CLIENT_ID] and q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["include_granted_scopes"] == ["true"]
    assert q["scope"][0].split() == connect.GOOGLE_SCOPES and q["redirect_uri"] == [CB]
    state_ = q["state"][0]
    with pytest.raises(ValueError, match="stale"):
        await connect.google_finish("good-code", "wrong-state")
    out = await connect.google_finish("good-code", state_)
    assert out["ok"] and "person3@gmail.com" in out["detail"] and "Calendar Trilok" in out["detail"]
    assert envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", world.env) == "rt-1" and envfile.get("ATLAS_GOOGLE_CONNECTED_AT", world.env)
    with pytest.raises(ValueError, match="stale"):
        await connect.google_finish("good-code", state_)                          # a state is used once
    g = (await connect.status())["google"]
    assert g["ok"] and g["client"] and g["consented"] and not g["needs_reconnect"] and g["connected_at"]
    assert g["client_id_prefix"] == "123.apps.goo"
    assert g["scope_words"] == ["read Gmail", "read the calendar"]  # read-only (docs/privacy.md)
    assert "7 days" in g["testing_note"]
    assert "consent started" in caplog.text and "connected (Gmail" in caplog.text
    for secret_word in (SECRET, "good-code", "rt-1"):
        assert secret_word not in caplog.text                                    # outcomes are logged, values never


@aio
async def test_every_token_endpoint_answer_has_a_fix_path(world, caplog):
    caplog.set_level(logging.INFO, logger="core.connect")
    connect.google_set_client(CLIENT_ID, SECRET)
    with pytest.raises(ValueError, match="expired or was already used"):
        await connect.google_finish("used-code", _state_for())
    with pytest.raises(ValueError, match="no refresh token"):
        await connect.google_finish("no-refresh-code", _state_for())
    with pytest.raises(ValueError, match=r"redirect URI http://127\.0\.0\.1:9999/api/connect/google/callback"):
        await connect.google_finish("good-code", _state_for("http://127.0.0.1:9999/api/connect/google/callback"))
    # the secret in .env is wrong: read at exchange time, refused by Google, named on the tile
    envfile.set_value("ATLAS_GOOGLE_CLIENT_SECRET", "GOCSPX-stale_fake", world.env)
    with pytest.raises(ValueError, match="Replace the OAuth client"):
        await connect.google_finish("good-code", _state_for())
    assert envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", world.env) == ""           # nothing half-written
    envfile.set_value("ATLAS_GOOGLE_CLIENT_SECRET", "", world.env)
    with pytest.raises(ValueError, match="save it again"):
        await connect.google_finish("good-code", _state_for())
    assert connect.describe_consent_error("access_denied") == "Consent was cancelled."
    assert "Google said: weird" in connect.describe_consent_error("weird")
    for word in (SECRET, "good-code", "used-code", "GOCSPX-stale"):
        assert word not in caplog.text


@aio
async def test_an_expired_refresh_token_asks_for_a_reconnect_once(world, monkeypatch):
    connect.google_set_client(CLIENT_ID, SECRET)
    envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", "rt-expired", world.env)
    monkeypatch.setattr(google_auth.settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(google_auth.settings, "google_client_secret", SECRET)
    monkeypatch.setattr(google_auth.settings, "google_refresh_token", "rt-expired")
    google_auth.invalidate()
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.path)
        return _provider(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(google_auth.GoogleNeedsConsent, match="Connect tab"):
            await google_auth.access_token(c)
        for _ in range(3):
            with pytest.raises(google_auth.GoogleNeedsConsent):
                await google_auth.access_token(c)
    assert len(hits) == 1 and google_auth.needs_reconnect()                      # one refusal, no retry loop
    g = (await connect.status())["google"]                                       # the live check sees it
    assert g["needs_reconnect"] and g["ok"] is False and "Reconnect Google" in g["detail"] and "7 days" in g["detail"]
    assert (await connect.status(check=False))["google"]["needs_reconnect"]      # this process remembers
    google_auth.invalidate()
    assert not (await connect.status(check=False))["google"]["needs_reconnect"]
    await state.record_failure("gmail", "auth: google refresh token is expired or revoked")
    assert (await connect.status(check=False))["google"]["needs_reconnect"]      # the worker's verdict reaches the web process
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", headers=HEADERS) as c:
        body = (await c.get("/api/health")).json()
        assert any("Reconnect Google" in w for w in body.get("warnings", []))   # and Home
    out = await connect.google_finish("good-code", _state_for())
    assert out["ok"] and not (await connect.status())["google"]["needs_reconnect"]


@aio
async def test_disconnect_revokes_and_forgets_but_keeps_the_client(world):
    connect.google_set_client(CLIENT_ID, SECRET)
    await connect.google_finish("good-code", _state_for())
    REVOKED.clear()
    assert await connect.google_disconnect() == {"ok": True, "had_consent": True, "revoked": True}
    assert REVOKED == ["rt-1"]
    assert envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", world.env) == "" and envfile.get("ATLAS_GOOGLE_CONNECTED_AT", world.env) == ""
    assert envfile.get("ATLAS_GOOGLE_CLIENT_SECRET", world.env) == SECRET
    g = (await connect.status())["google"]
    assert g["client"] and not g["consented"] and g["ok"] is None and "consent" in g["detail"]
    assert await connect.google_disconnect() == {"ok": True, "had_consent": False, "revoked": False} and REVOKED == ["rt-1"]
    # replacing the client drops the consent as well: the token belonged to the old client
    await connect.google_finish("good-code", _state_for())
    assert connect.google_set_client(CLIENT_ID, SECRET)["tokens_cleared"] is True
    assert envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", world.env) == ""
    assert not (await connect.status(check=False))["google"]["consented"]


@aio
async def test_a_zapier_url_brings_its_apps_to_the_ceo(world):
    checked = await connect.check_mcp("https://mcp.zapier.com/api/mcp/s/abc/mcp")
    assert checked == {"ok": True, "server": "Zapier MCP", "version": "1.2", "protocol": "2025-06-18",
                       "tools": ["gmail_find_email", "google_calendar_create_event", "notion_search"], "count": 3}
    with pytest.raises(ValueError, match="https"):
        await connect.check_mcp("http://mcp.zapier.com/x")
    with pytest.raises(ValueError, match="refused"):
        await connect.check_mcp("https://mcp.zapier.com/x", token="z-bad")
    with pytest.raises(ValueError, match="not an MCP server"):
        await connect.check_mcp("https://broken.example/mcp")

    added = await connect.add_mcp_server("Zapier", "https://mcp.zapier.com/api/mcp/s/abc/mcp", None, ["ceo", "coo"])
    assert added["name"] == "zapier" and added["agents"] == ["ceo", "coo"] and added["count"] == 3
    stored = json.loads(envfile.get("ATLAS_MCP_SERVERS", world.env))
    assert stored == [{"name": "zapier", "url": "https://mcp.zapier.com/api/mcp/s/abc/mcp", "agents": ["ceo", "coo"], "tool_count": 3}]
    assert connect.mcp_servers() == [{"name": "zapier", "host": "mcp.zapier.com", "agents": ["ceo", "coo"], "allowed_tools": [],
                                      "has_token": False, "tool_count": 3}]
    again = await connect.add_mcp_server("zapier", "https://mcp.zapier.com/api/mcp/s/abc/mcp", "z-good", None)
    stored = json.loads(envfile.get("ATLAS_MCP_SERVERS", world.env))
    assert len(stored) == 1 and stored[0]["token"] == "z-good" and stored[0]["agents"] == ["ceo"] and again["servers"][0]["has_token"]
    with pytest.raises(ValueError, match="server name"):
        await connect.add_mcp_server("Not Valid!", "https://mcp.zapier.com/x")
    assert connect.remove_mcp_server("zapier")["servers"] == [] and envfile.get("ATLAS_MCP_SERVERS", world.env) == ""
    with pytest.raises(LookupError):
        connect.remove_mcp_server("zapier")


@aio
async def test_routes(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", headers=HEADERS) as c:
        r = await c.get("/api/connect", params={"check": "false"})
        assert r.status_code == 200 and r.json()["checked"] is False
        assert (await c.get("/api/connect/google/start")).status_code == 422          # no client yet
        r = await c.post("/api/connect/google/client", json={"client_id": CLIENT_ID, "client_secret": CLIENT_ID})
        assert r.status_code == 422 and "client id pasted as the secret" in r.json()["detail"]
        r = await c.post("/api/connect/google/client", json={"client_id": CLIENT_ID, "client_secret": SECRET})
        assert r.status_code == 200 and r.json()["client_id_prefix"] == "123.apps.goo"
        r = await c.get("/api/connect/google/start")
        assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["redirect_uri"] == [CB] and q["prompt"] == ["consent"] and q["access_type"] == ["offline"]
        r = await c.get("/api/connect/google/callback", params={"error": "access_denied"})
        assert r.status_code == 200 and "did not connect" in r.text and "Consent was cancelled." in r.text
        r = await c.get("/api/connect/google/callback", params={"code": "good-code", "state": q["state"][0]})
        assert r.status_code == 200 and "Google is connected" in r.text and "person3@gmail.com" in r.text
        r = await c.get("/api/connect")
        assert r.json()["google"]["ok"] and r.json()["google"]["consented"]
        r = await c.post("/api/connect/google/disconnect", json={})
        assert r.status_code == 200 and r.json() == {"ok": True, "had_consent": True, "revoked": True}
        assert (await c.get("/api/connect?check=false")).json()["google"]["consented"] is False
        envfile.set_value("ATLAS_GOOGLE_CLIENT_SECRET", "GOCSPX-wrong_fake", world.env)
        state_ = parse_qs(urlparse((await c.get("/api/connect/google/start")).headers["location"]).query)["state"][0]
        r = await c.get("/api/connect/google/callback", params={"code": "good-code", "state": state_})
        assert "did not connect" in r.text and "Replace the OAuth client on this tile" in r.text and "content='15;" in r.text
        r = await c.post("/api/connect/github", json={"token": "gh-good"})
        assert r.status_code == 200 and r.json()["repos"]
        assert (await c.post("/api/connect/github", json={"token": "gh-bad"})).status_code == 422
        assert (await c.post("/api/connect/canvas", json={"url": "", "token": "cv-good"})).status_code == 200
        assert (await c.post("/api/connect/alarm", json={"url": "https://hc-ping.com/abc"})).status_code == 200
        r = await c.post("/api/connect/mcp", json={"name": "zapier", "url": "https://mcp.zapier.com/api/mcp/s/abc/mcp"})
        assert r.status_code == 200 and r.json()["count"] == 3
        assert (await c.post("/api/connect/mcp/remove", json={"name": "zapier"})).status_code == 200
        assert (await c.post("/api/connect/mcp/remove", json={"name": "zapier"})).status_code == 404
        r = await c.get("/api/connect")
        assert r.json()["github"]["ok"] and r.json()["canvas"]["ok"] and r.json()["alarm"]["ok"]


@aio
async def test_the_worker_reloads_when_env_changes(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(envwatch.config, "reload", lambda p: calls.append(str(p)))
    envwatch._seen.clear()
    assert await envwatch.tick(env) is False                    # the first look only remembers
    assert await envwatch.tick(env) is False                    # unchanged
    import os
    import time

    os.utime(env, (time.time() + 5, time.time() + 5))
    assert await envwatch.tick(env) is True and calls == [str(env)]
    assert await envwatch.tick(env) is False
    assert await envwatch.tick(tmp_path / "missing.env") is False
