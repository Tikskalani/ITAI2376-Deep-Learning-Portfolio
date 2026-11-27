"""Atlas -- Google OAuth for the Gmail and Calendar connectors.

Plain httpx against the token endpoint; no Google SDK. The one-time
consent happens on the laptop from the Connect tab (core/connect.py; the
older scripts/google_auth.py does the same from a terminal) and lands a
refresh token in .env. From then on this module trades the refresh token
for short-lived access tokens and caches them in memory.

The operational trap, stated plainly (revival plan 5.3): a Google Cloud
project in "testing" mode expires refresh tokens after seven days. When
that happens the token endpoint answers invalid_grant. The token is then
remembered as rejected and no call retries it (one refusal, not a loop);
the connectors record the failure, the owner gets one high-priority
notification a day pointing at Reconnect Google, the tile and Home say
"needs reconnect", and nothing else breaks. A new token written through
the Connect tab (which calls invalidate()) lifts the block. Publishing
the app (or using a Workspace account) removes the expiry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from core.config import settings
from core.connectors.base import ConnectorError, TransientConnectorError

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT = httpx.Timeout(20.0)
# Refresh a little before Google's stated expiry so a request never races it.
_EARLY_SECONDS = 60

TokenProvider = Callable[[], Awaitable[str]]


class GoogleAuthError(ConnectorError):
    """The token endpoint refused us for a reason that will not go away."""


class GoogleNeedsConsent(GoogleAuthError):
    """invalid_grant: the refresh token is revoked or expired; re-run consent."""


RECONNECT_HINT = "reconnect Google on the Connect tab (a project in Testing mode expires refresh tokens after 7 days)"

_cache: dict[str, tuple[str, float]] = {}
_rejected: set[str] = set()   # refresh tokens Google answered invalid_grant for; not retried until a new one is written


def configured() -> bool:
    return bool(
        settings.google_client_id and settings.google_client_secret and settings.google_refresh_token
    )


def invalidate() -> None:
    _cache.clear()
    _rejected.clear()


def needs_reconnect() -> bool:
    """True once Google has rejected the current refresh token (expired or revoked)."""
    return bool(settings.google_refresh_token) and settings.google_refresh_token in _rejected


async def access_token(client: httpx.AsyncClient | None = None) -> str:
    """A valid access token, refreshed when the cached one is near expiry."""
    if not configured():
        raise GoogleAuthError("google oauth is not configured (ATLAS_GOOGLE_*)")
    key = settings.google_refresh_token
    if key in _rejected:
        raise GoogleNeedsConsent(f"google refresh token was rejected by Google earlier; {RECONNECT_HINT}")
    cached = _cache.get(key)
    now = time.time()
    if cached and cached[1] - _EARLY_SECONDS > now:
        return cached[0]

    own = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": key,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise TransientConnectorError(f"google token endpoint: {exc}") from exc
    finally:
        if own:
            await client.aclose()

    if resp.status_code >= 500 or resp.status_code == 429:
        raise TransientConnectorError(f"google token endpoint: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", "")
        except ValueError:
            err = resp.text[:100]
        if err == "invalid_grant":
            _rejected.add(key)
            raise GoogleNeedsConsent(f"google refresh token is expired or revoked; {RECONNECT_HINT}")
        if err == "invalid_client":
            raise GoogleAuthError("google rejected the client secret; replace the OAuth client on the Connect tab")
        raise GoogleAuthError(f"google token endpoint: HTTP {resp.status_code} {err}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise GoogleAuthError("google token endpoint returned no access_token")
    _cache[key] = (token, now + float(data.get("expires_in") or 3600))
    return token


async def default_token_provider() -> str:
    return await access_token()


__all__ = [
    "TOKEN_URL",
    "TokenProvider",
    "GoogleAuthError",
    "GoogleNeedsConsent",
    "configured",
    "invalidate",
    "needs_reconnect",
    "access_token",
    "default_token_provider",
]
