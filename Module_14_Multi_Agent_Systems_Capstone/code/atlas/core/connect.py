"""Atlas -- Connect: one tap per app, from the dashboard.

scripts/connect.py is a wizard he never finished, so nothing was ever
connected and nine of thirteen agents have never run. This is the same
work as a screen: a tile per source with its state, the checks the wizard
ran, and the write into .env, followed by a settings reload so the web
process and the worker use the new token without a restart.

  google   the loopback OAuth consent, from the laptop's own browser
           (Desktop clients may redirect to 127.0.0.1 on any port); the
           client can be replaced and the consent dropped from the same tile
  github   a fine-grained token, proved against /user, the repos listed
  canvas   the Canvas base URL and a token, proved against /users/self
  alarm    a healthchecks.io ping URL, pinged once
  mcp      any MCP server over streamable HTTP: Zapier's turns the apps he
           has connected there into tools for the CEO in one paste;
           Notion's, Composio's and the rest work the same way

Nothing here echoes a token back: status says connected or not and why,
and the log lines carry outcomes, never a code, a secret or a token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from core import config, envfile
from core.config import settings
from core.connectors import google_auth
from core.connectors.state import list_states
from core.version import VERSION
from scripts.connect import (
    CANVAS_DEFAULT,
    Result,
    check_alarm,
    check_canvas,
    check_github,
    check_google,
    github_repos,
)

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Read-only (docs/privacy.md): no drafts, no calendar writes. A tool that would
# need a write scope fails with a clear message instead.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
MCP_PROTOCOL = "2025-06-18"
_STATE_TTL_S = 600
_TIMEOUT = httpx.Timeout(20.0)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

def _network_client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT)


# Tests hand these a MockTransport; the dashboard uses the network.
CLIENT_FACTORY: Callable[[], httpx.Client] = _network_client
ASYNC_TRANSPORT: Any = None

_pending: dict[str, dict[str, Any]] = {}   # OAuth state nonce -> {redirect, at}


def _reload() -> None:
    config.reload(envfile.PATH)
    google_auth.invalidate()


def _write(key: str, value: str) -> None:
    envfile.set_value(key, value, envfile.PATH)
    _reload()


def _res(r: Result) -> dict[str, Any]:
    return {"ok": r.ok, "detail": r.detail}


# ------------------------------------------------------------------ status


async def status(*, check: bool = True) -> dict[str, Any]:
    """Every tile's state. check=True proves each token against its provider."""
    env = envfile.read(envfile.PATH)
    states = {s["source"]: s for s in await list_states()}
    client = CLIENT_FACTORY() if check else None
    try:
        google, canvas, github, alarm = await asyncio.gather(
            asyncio.to_thread(check_google, env.get("ATLAS_GOOGLE_CLIENT_ID", ""), env.get("ATLAS_GOOGLE_CLIENT_SECRET", ""),
                              env.get("ATLAS_GOOGLE_REFRESH_TOKEN", ""), client) if check else _skip("Google", env, "ATLAS_GOOGLE_REFRESH_TOKEN"),
            asyncio.to_thread(check_canvas, env.get("ATLAS_CANVAS_URL", ""), env.get("ATLAS_CANVAS_TOKEN", ""), client) if check else _skip("Canvas", env, "ATLAS_CANVAS_TOKEN"),
            asyncio.to_thread(check_github, env.get("ATLAS_GITHUB_TOKEN", ""), client) if check else _skip("GitHub", env, "ATLAS_GITHUB_TOKEN"),
            asyncio.to_thread(check_alarm, env.get("ATLAS_HEARTBEAT_URL", ""), client) if check else _skip("Alarm", env, "ATLAS_HEARTBEAT_URL"),
        )
    finally:
        if client is not None:
            client.close()
    return {
        "checked": check,
        "google": _google_block(google, env, states, checked=check),
        "canvas": {**_res(canvas), "url": env.get("ATLAS_CANVAS_URL") or CANVAS_DEFAULT,
                   "configured": bool(env.get("ATLAS_CANVAS_TOKEN")), "state": _state(states.get("canvas"))},
        "github": {**_res(github), "configured": bool(env.get("ATLAS_GITHUB_TOKEN")), "state": _state(states.get("github"))},
        "alarm": {**_res(alarm), "configured": bool(env.get("ATLAS_HEARTBEAT_URL"))},
        "mcp": {"servers": mcp_servers()},
    }


async def _skip(name: str, env: dict[str, str], key: str) -> Result:
    return Result(name, True if env.get(key) else None, "configured (not checked)" if env.get(key) else "not connected")


def _state(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"last_ok_at": row.get("last_ok_at"), "last_error": row.get("last_error"),
            "failures": row.get("consecutive_failures") or row.get("failures")}


# ------------------------------------------------------------------ google

REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CONSOLE_CLIENTS = "Google Cloud > APIs & Services > Credentials > Clients > Atlas desktop"
TESTING_NOTE = "Tokens expire every 7 days until the app is published in Google Cloud."
RECONNECT_DETAIL = "Google needs a reconnect: the refresh token expired or was revoked. Tap Reconnect Google. " + TESTING_NOTE
GOOGLE_SCOPE_WORDS = {
    "https://www.googleapis.com/auth/gmail.readonly": "read Gmail",
    "https://www.googleapis.com/auth/calendar.readonly": "read the calendar",
}
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com$")
_SECRET = re.compile(r"^GOCSPX-[A-Za-z0-9_-]+$")
_EXCHANGE_ERRORS = {
    "invalid_client": "Google rejected the client secret. Replace the OAuth client on this tile with the current "
                      f"secret from {CONSOLE_CLIENTS}.",
    "invalid_grant": "The sign-in code expired or was already used, or the refresh token was revoked or expired. Reconnect.",
    "access_denied": "Consent was cancelled.",
    "unauthorized_client": f"This OAuth client may not use the desktop flow; it has to be of type Desktop app ({CONSOLE_CLIENTS}).",
    "invalid_request": "Google called the request malformed; start the tap again from the Connect tab.",
}


def _clean(value: str, what: str) -> str:
    """A pasted value as Google issued it: edge whitespace and one pair of
    wrapping quotes off, no whitespace inside, never a masked copy."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        raise ValueError(f"paste the {what}")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"the {what} has a space or line break inside it; copy it again as one piece")
    if "*" in value or "•" in value:
        raise ValueError(f"that is a masked copy of the {what} (the dots stand in for it); use the copy icon in {CONSOLE_CLIENTS}")
    return value


def google_set_client(client_id: str, client_secret: str) -> dict[str, Any]:
    """Save or replace the Desktop OAuth client. Saving drops the stored
    consent: a refresh token belongs to the client that issued it."""
    client_id = _clean(client_id, "client id")
    client_secret = _clean(client_secret, "client secret")
    if not _CLIENT_ID.match(client_id):
        raise ValueError(f"the client id ends in .apps.googleusercontent.com; copy it from {CONSOLE_CLIENTS}")
    if client_secret == client_id or client_secret.endswith(".apps.googleusercontent.com"):
        raise ValueError("that is the client id pasted as the secret; the client secret starts with GOCSPX- "
                         f"and sits beside the id in {CONSOLE_CLIENTS}")
    if not _SECRET.match(client_secret):
        raise ValueError(f"a Desktop client secret starts with GOCSPX-; copy it with the copy icon in {CONSOLE_CLIENTS}")
    had_consent = bool(envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", envfile.PATH))
    envfile.set_value("ATLAS_GOOGLE_CLIENT_ID", client_id, envfile.PATH)
    envfile.set_value("ATLAS_GOOGLE_CLIENT_SECRET", client_secret, envfile.PATH)
    if had_consent:
        envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", "", envfile.PATH)
        envfile.set_value("ATLAS_GOOGLE_CONNECTED_AT", "", envfile.PATH)
    _reload()
    log.info("google connect: oauth client saved (id %s..., secret of %d chars%s)",
             client_id[:12], len(client_secret), "; the stored consent was dropped" if had_consent else "")
    return {"client": True, "client_id_prefix": client_id[:12], "consented": False, "tokens_cleared": had_consent}


def google_auth_url(redirect_uri: str) -> str:
    """The consent URL. A Desktop client may redirect to 127.0.0.1 on any port.
    access_type=offline and prompt=consent make Google hand out a refresh
    token every time, not only on the first consent."""
    client_id = envfile.get("ATLAS_GOOGLE_CLIENT_ID", envfile.PATH)
    if not client_id:
        raise ValueError("add the OAuth client id and secret first")
    host = httpx.URL(redirect_uri).host
    if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
        raise ValueError("Google consent has to start from the laptop's own browser (127.0.0.1); open Atlas there for this one tap")
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > _STATE_TTL_S]:
        _pending.pop(k, None)
    _pending[state] = {"redirect": redirect_uri, "at": now}
    log.info("google connect: consent started, redirect %s", redirect_uri)
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
    })


def _exchange_error(status: int, body: dict[str, Any], redirect: str) -> str:
    err = str(body.get("error") or "")
    if err == "redirect_uri_mismatch":
        return (f"Google rejected the redirect URI {redirect}. A Desktop app client accepts any 127.0.0.1 port; "
                f"check the client's type in {CONSOLE_CLIENTS}.")
    if err in _EXCHANGE_ERRORS:
        return _EXCHANGE_ERRORS[err]
    desc = str(body.get("error_description") or "").strip()
    return f"Google answered HTTP {status}" + (f" {err}" if err else "") + (f" ({desc})" if desc else "") + ". Try the tap again."


def describe_consent_error(error: str) -> str:
    """The error Google puts on the callback URL when consent did not happen."""
    return _EXCHANGE_ERRORS.get(error, f"Google said: {error}. Try the tap again.")


def _friendly_check(detail: str) -> str:
    """scripts/connect.py's refresh-path verdicts in the tile's words."""
    if detail.startswith("consent expired"):
        return RECONNECT_DETAIL
    if detail.startswith("client id or secret"):
        return _EXCHANGE_ERRORS["invalid_client"]
    return detail


async def google_finish(code: str, state: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """The callback: trade the code for a refresh token, prove it with one
    cheap Gmail call and one cheap Calendar call, keep it. The client id and
    secret are read from .env at this moment, not from a cache."""
    pending = _pending.pop(state or "", None)
    if pending is None:
        raise ValueError("the consent link is stale; start again from the Connect tab")
    env = envfile.read(envfile.PATH)
    client_id, client_secret = env.get("ATLAS_GOOGLE_CLIENT_ID", ""), env.get("ATLAS_GOOGLE_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise ValueError("the OAuth client is gone from .env; save it again on this tile")
    c = client or CLIENT_FACTORY()
    try:
        try:
            resp = await asyncio.to_thread(
                c.post, google_auth.TOKEN_URL,
                data={"code": code, "client_id": client_id, "client_secret": client_secret,
                      "redirect_uri": pending["redirect"], "grant_type": "authorization_code"},
            )
        except httpx.HTTPError as exc:
            log.warning("google connect: token endpoint unreachable (%s)", type(exc).__name__)
            raise ValueError(f"could not reach Google's token endpoint ({type(exc).__name__}); try the tap again") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code != 200:
            log.info("google connect: token exchange refused, HTTP %s %s", resp.status_code, body.get("error") or "")
            raise ValueError(_exchange_error(resp.status_code, body, pending["redirect"]))
        refresh = body.get("refresh_token")
        if not refresh:
            log.info("google connect: token exchange came back without a refresh token")
            raise ValueError("Google returned no refresh token. Reconnect (Atlas now forces consent).")
        result = await asyncio.to_thread(check_google, client_id, client_secret, refresh, c)
    finally:
        if client is None:
            c.close()
    if not result.ok:
        log.info("google connect: the new token did not prove out: %s", result.detail)
        raise ValueError(_friendly_check(result.detail))
    envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", refresh, envfile.PATH)
    envfile.set_value("ATLAS_GOOGLE_CONNECTED_AT", datetime.now(UTC).isoformat(timespec="seconds"), envfile.PATH)
    _reload()
    log.info("google connect: connected (%s)", result.detail)
    return {"ok": True, "detail": result.detail}


async def google_disconnect(*, client: httpx.Client | None = None) -> dict[str, Any]:
    """Drop the consent: revoke the refresh token at Google (best effort) and
    delete it here. The OAuth client stays, so Reconnect is one tap."""
    refresh = envfile.get("ATLAS_GOOGLE_REFRESH_TOKEN", envfile.PATH)
    revoked = False
    if refresh:
        c = client or CLIENT_FACTORY()
        try:
            resp = await asyncio.to_thread(c.post, REVOKE_URL, data={"token": refresh})
            revoked = resp.status_code == 200
        except httpx.HTTPError as exc:
            log.info("google connect: revoke skipped, Google unreachable (%s)", type(exc).__name__)
        finally:
            if client is None:
                c.close()
    envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", "", envfile.PATH)
    envfile.set_value("ATLAS_GOOGLE_CONNECTED_AT", "", envfile.PATH)
    _reload()
    log.info("google connect: disconnected (token revoked at Google: %s)", revoked)
    return {"ok": True, "had_consent": bool(refresh), "revoked": revoked}


def _google_block(result: Result, env: dict[str, str], states: dict[str, Any], *, checked: bool) -> dict[str, Any]:
    """The Google tile. needs_reconnect comes from the live refresh when the
    status was checked, else from what the worker's connectors recorded."""
    client_id = env.get("ATLAS_GOOGLE_CLIENT_ID", "")
    consented = bool(env.get("ATLAS_GOOGLE_REFRESH_TOKEN"))
    gmail, gcal = _state(states.get("gmail")), _state(states.get("gcal"))
    auth_failed = any(str((s or {}).get("last_error") or "").startswith("auth:") for s in (gmail, gcal))
    live_expired = checked and result.ok is False and result.detail.startswith("consent expired")
    needs_reconnect = consented and (live_expired or (not checked and (auth_failed or google_auth.needs_reconnect())))
    detail = _friendly_check(result.detail) if result.ok is False else result.detail
    if needs_reconnect:
        detail = RECONNECT_DETAIL
    refreshes = [s["last_ok_at"] for s in (gmail, gcal) if s and s.get("last_ok_at")]
    return {
        "ok": False if needs_reconnect else result.ok, "detail": detail,
        "client": bool(client_id and env.get("ATLAS_GOOGLE_CLIENT_SECRET")), "client_id_prefix": client_id[:12],
        "consented": consented, "needs_reconnect": needs_reconnect,
        "connected_at": env.get("ATLAS_GOOGLE_CONNECTED_AT") or None, "last_refresh": max(refreshes) if refreshes else None,
        "scopes": GOOGLE_SCOPES, "scope_words": [GOOGLE_SCOPE_WORDS[sc] for sc in GOOGLE_SCOPES],
        "testing_note": TESTING_NOTE, "gmail": gmail, "calendar": gcal,
    }


# ------------------------------------------------------------------ paste-once sources


async def set_github(token: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    token = token.strip()
    if not token:
        raise ValueError("paste the token")
    c = client or CLIENT_FACTORY()
    try:
        result = await asyncio.to_thread(check_github, token, c)
        repos = await asyncio.to_thread(github_repos, token, c) if result.ok else []
    finally:
        if client is None:
            c.close()
    if not result.ok:
        raise ValueError(result.detail)
    _write("ATLAS_GITHUB_TOKEN", token)
    return {"ok": True, "detail": result.detail, "repos": repos}


async def set_canvas(url: str, token: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    url = (url or CANVAS_DEFAULT).strip().rstrip("/")
    token = token.strip()
    if not url.startswith("https://"):
        raise ValueError("the Canvas address must start with https://")
    if not token:
        raise ValueError("paste the token")
    c = client or CLIENT_FACTORY()
    try:
        result = await asyncio.to_thread(check_canvas, url, token, c)
    finally:
        if client is None:
            c.close()
    if not result.ok:
        raise ValueError(result.detail)
    envfile.set_value("ATLAS_CANVAS_URL", url, envfile.PATH)
    _write("ATLAS_CANVAS_TOKEN", token)
    return {"ok": True, "detail": result.detail, "url": url}


async def set_alarm(url: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    url = url.strip()
    c = client or CLIENT_FACTORY()
    try:
        result = await asyncio.to_thread(check_alarm, url, c)
    finally:
        if client is None:
            c.close()
    if not result.ok:
        raise ValueError(result.detail)
    _write("ATLAS_HEARTBEAT_URL", url)
    return {"ok": True, "detail": result.detail}


# ------------------------------------------------------------------ mcp servers


def mcp_servers() -> list[dict[str, Any]]:
    """The configured servers, tokens never included."""
    raw = (envfile.get("ATLAS_MCP_SERVERS", envfile.PATH) or settings.mcp_servers_json or "").strip()
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("name") or not e.get("url"):
            continue
        out.append({"name": e["name"], "host": httpx.URL(e["url"]).host, "agents": e.get("agents") or ["ceo"],
                    "allowed_tools": e.get("allowed_tools") or [], "has_token": bool(e.get("token")),
                    "tool_count": e.get("tool_count")})
    return out


# What a server lets Atlas do, grouped by app for the Links room. Zapier names
# its tools <app>_<action>; the known app prefixes are matched longest first,
# anything else groups by its first word.
_APPS = {
    "google_calendar": "Google Calendar", "google_contacts": "Google Contacts", "google_docs": "Google Docs",
    "google_drive": "Google Drive", "google_sheets": "Google Sheets", "google_tasks": "Google Tasks",
    "microsoft_outlook": "Outlook", "gmail": "Gmail", "github": "GitHub", "slack": "Slack", "notion": "Notion",
    "zoom": "Zoom", "linkedin": "LinkedIn", "trello": "Trello", "asana": "Asana", "hubspot": "HubSpot",
    "airtable": "Airtable", "dropbox": "Dropbox", "calendly": "Calendly", "todoist": "Todoist", "canva": "Canva",
}
_HELPERS = {"get_dynamic_properties_schema", "list_dynamic_enum_values", "get_configuration_url"}
_TOOLS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TOOLS_TTL = 600.0


def _sentence(words: str) -> str:
    text = words.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else text


def group_tools(names: list[str]) -> list[dict[str, Any]]:
    """[{app, tools: [{name, action}]}], apps by name, Zapier's helpers last."""
    groups: dict[str, list[dict[str, str]]] = {}
    for name in names:
        if name in _HELPERS:
            app, action = "Zapier helpers", _sentence(name)
        else:
            prefix = next((p for p in sorted(_APPS, key=len, reverse=True) if name.startswith(p + "_")), None)
            if prefix:
                app, action = _APPS[prefix], _sentence(name[len(prefix) + 1:])
            elif "_" in name:
                head, rest = name.split("_", 1)
                app, action = _sentence(head), _sentence(rest)
            else:
                app, action = "Other", _sentence(name)
        groups.setdefault(app, []).append({"name": name, "action": action})
    order = sorted(groups, key=lambda a: (a in ("Zapier helpers", "Other"), a == "Zapier helpers", a.lower()))
    return [{"app": a, "tools": sorted(groups[a], key=lambda t: t["action"])} for a in order]


async def mcp_tools(name: str) -> dict[str, Any]:
    """One configured server's tools, grouped by app. Asks the server at most
    once every ten minutes; the URL and the token never leave this module."""
    raw = (envfile.get("ATLAS_MCP_SERVERS", envfile.PATH) or settings.mcp_servers_json or "").strip()
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    entry = next((e for e in entries if isinstance(e, dict) and str(e.get("name") or "").lower() == name.lower()
                  and e.get("url")), None)
    if entry is None:
        raise LookupError(f"no MCP server named {name} is saved; add it again in Settings, Connections (name and URL)")
    now = time.monotonic()
    hit = _TOOLS_CACHE.get(name)
    if hit and now - hit[0] < _TOOLS_TTL:
        return hit[1]
    checked = await check_mcp(entry["url"], entry.get("token"))
    out = {"name": name, "server": checked.get("server"), "count": checked.get("count", len(checked.get("tools") or [])),
           "groups": group_tools(list(checked.get("tools") or [])), "checked_at": datetime.now(UTC).isoformat()}
    _TOOLS_CACHE[name] = (now, out)
    return out


def _parse_sse_or_json(resp: httpx.Response) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/event-stream"):
        last: dict[str, Any] = {}
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return last
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def check_mcp(url: str, token: str | None = None) -> dict[str, Any]:
    """initialize, initialized, tools/list over streamable HTTP. Returns the
    server's name and its tools, or raises ValueError with the reason."""
    url = url.strip()
    if not url.startswith("https://"):
        raise ValueError("the MCP URL must start with https://")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "atlas", "version": VERSION}}}
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=ASYNC_TRANSPORT, follow_redirects=True) as c:
        try:
            r = await c.post(url, json=init, headers=headers)
        except httpx.HTTPError as exc:
            raise ValueError(f"could not reach the server: {exc}") from exc
        if r.status_code in (401, 403):
            raise ValueError("the server refused the token" if token else "the server wants a token")
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code} from the server")
        body = _parse_sse_or_json(r)
        result = body.get("result") or {}
        if "error" in body or not result:
            raise ValueError(f"not an MCP server: {body.get('error', {}).get('message') if isinstance(body.get('error'), dict) else 'no initialize result'}")
        session = r.headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        await c.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        r2 = await c.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=headers)
        tools = [t.get("name") for t in ((_parse_sse_or_json(r2).get("result") or {}).get("tools") or []) if t.get("name")]
    info = result.get("serverInfo") or {}
    return {"ok": True, "server": info.get("name") or httpx.URL(url).host, "version": info.get("version"),
            "protocol": result.get("protocolVersion"), "tools": tools[:200], "count": len(tools)}


async def add_mcp_server(name: str, url: str, token: str | None = None, agents: list[str] | None = None) -> dict[str, Any]:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError("a server name is letters, digits, - or _ (zapier, notion, composio)")
    checked = await check_mcp(url, token)
    raw = (envfile.get("ATLAS_MCP_SERVERS", envfile.PATH) or "").strip()
    try:
        entries = [e for e in (json.loads(raw) if raw else []) if isinstance(e, dict) and e.get("name") != name]
    except ValueError:
        entries = []
    agents = [a.strip().lower() for a in (agents or ["ceo"]) if a.strip()] or ["ceo"]
    entry: dict[str, Any] = {"name": name, "url": url.strip(), "agents": agents, "tool_count": checked["count"]}
    if token and token.strip():
        entry["token"] = token.strip()
    entries.append(entry)
    _write("ATLAS_MCP_SERVERS", json.dumps(entries, ensure_ascii=False))
    return {**checked, "name": name, "agents": agents, "servers": mcp_servers()}


def remove_mcp_server(name: str) -> dict[str, Any]:
    raw = (envfile.get("ATLAS_MCP_SERVERS", envfile.PATH) or "").strip()
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    kept = [e for e in entries if isinstance(e, dict) and e.get("name") != name]
    if len(kept) == len(entries):
        raise LookupError(f"no server named {name}")
    _write("ATLAS_MCP_SERVERS", json.dumps(kept, ensure_ascii=False) if kept else "")
    return {"removed": name, "servers": mcp_servers()}


__all__ = ["GOOGLE_SCOPES", "TESTING_NOTE", "status", "google_set_client", "google_auth_url", "google_finish",
           "google_disconnect", "describe_consent_error", "set_github", "set_canvas", "set_alarm", "mcp_servers",
           "check_mcp", "add_mcp_server", "remove_mcp_server"]
