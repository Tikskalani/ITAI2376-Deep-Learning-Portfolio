"""Atlas -- the request guard in front of every route.

Atlas has no login: identity comes from the network edge (only Trilok's
signed-in Tailscale devices reach it). That keeps strangers out but not
a hostile page open in his own browser, which can still send requests to
a tailnet address. This guard closes that gap for writes:

  * Every POST, PUT, PATCH or DELETE under /api/ must carry the header
    X-Atlas-Client: 1. The dashboard adds it to every write. A page on
    another origin cannot add a custom header without a CORS preflight,
    and Atlas answers no preflight, so the browser never sends the write.
  * If the request carries an Origin, its host must be the host the
    request was sent to (Host or X-Forwarded-Host), loopback, or one
    listed in ATLAS_ALLOWED_ORIGINS. Belt and braces with the header.
  * Bodies are capped (Content-Length and the actual bytes read): 2 MB for
    the API, the upload limit plus headroom for /api/upload.
  * Every response gets nosniff, no-referrer, frame denial and a
    permissions policy that leaves only the microphone (voice input).

Token-authenticated machine endpoints (/api/ingest for the iOS Shortcut,
/mcp for Claude connectors) skip the header and origin checks; their
bearer token is the proof. ATLAS_WRITE_GUARD=false turns the write checks
off as an escape hatch; the size caps and headers always apply.

What this does not stop: code running inside the dashboard's own origin
(a browser extension or agent driving the Atlas tab). That needs a real
per-device credential; see docs/atlas-local-host.md.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

from core.config import settings

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_ROUTES = ("/api/ingest", "/mcp")
CLIENT_HEADER = b"x-atlas-client"
API_MAX_BYTES = 2 * 1024 * 1024
UPLOAD_MAX_BYTES = 10 * 1024 * 1024 + 256 * 1024  # the file cap plus multipart overhead
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), payment=(), usb=(), microphone=(self)"),
]


class _TooLarge(Exception):
    pass


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).netloc
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _allowed_hosts(headers: dict[bytes, bytes]) -> set[str]:
    hosts = set(LOOPBACK)
    for name in (b"host", b"x-forwarded-host"):
        raw = headers.get(name, b"").decode("latin-1")
        for part in raw.split(","):
            if part.strip():
                hosts.add(_host_only(part))
    for extra in (settings.allowed_origins or "").split(","):
        if extra.strip():
            hosts.add(_host_only(extra))
    return hosts


def body_limit(path: str) -> int:
    return UPLOAD_MAX_BYTES if path == "/api/upload" else API_MAX_BYTES


def write_refusal(method: str, path: str, headers: dict[bytes, bytes]) -> str | None:
    """Why this write must be refused, or None. Pure, for tests."""
    if method not in UNSAFE or not path.startswith("/api/") or not settings.write_guard:
        return None
    if path.startswith(TOKEN_ROUTES):
        return None
    origin = headers.get(b"origin", b"").decode("latin-1").strip()
    if origin and origin != "null" and _host_only(origin) not in _allowed_hosts(headers):
        return f"cross-origin write refused (Origin {origin})"
    if origin == "null":
        return "cross-origin write refused (opaque origin)"
    if headers.get(CLIENT_HEADER) != b"1":
        return "write refused: missing X-Atlas-Client header (only the Atlas dashboard may write)"
    return None


async def _json(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    *SECURITY_HEADERS],
    })
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        headers = {k.lower(): v for k, v in scope.get("headers") or []}

        reason = write_refusal(method, path, headers)
        if reason:
            await _json(send, 403, reason)
            return

        limit = body_limit(path)
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await _json(send, 413, f"request body over {limit // 1024} KB")
                    return
            except ValueError:
                await _json(send, 400, "bad Content-Length")
                return

        seen = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return message

        async def secured_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in SECURITY_HEADERS if k not in existing
                ]
            await send(message)

        try:
            await self.app(scope, limited_receive, secured_send)
        except _TooLarge:
            if not started:
                await _json(send, 413, f"request body over {limit // 1024} KB")


__all__ = ["API_MAX_BYTES", "CLIENT_HEADER", "RequestGuard", "body_limit", "write_refusal"]
