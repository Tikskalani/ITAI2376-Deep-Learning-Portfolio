"""One-time Google consent for the Gmail and Calendar connectors.

Run on the laptop, from the repo root, after creating an OAuth client:

  1. Google Cloud console, APIs & Services, enable the Gmail API and the
     Google Calendar API.
  2. OAuth consent screen: External, Testing, add your own address as a
     test user (testing mode expires refresh tokens every 7 days; publish
     the app to stop that).
  3. Credentials, Create credentials, OAuth client ID, type "Desktop app".
     Put the client id and secret in .env as ATLAS_GOOGLE_CLIENT_ID and
     ATLAS_GOOGLE_CLIENT_SECRET.
  4. python -m scripts.google_auth --write-env
     A browser opens (or paste the printed URL), you approve, and the
     script saves ATLAS_GOOGLE_REFRESH_TOKEN into .env. Restart the worker.
     Without --write-env it prints the token for you to paste instead.

scripts/connect.bat walks through all of this (and Canvas and GitHub).

Scopes: gmail.readonly and calendar.readonly only (docs/privacy.md). Atlas
creates no drafts and no calendar events; a tool that would needs a write
scope and fails with a clear message instead.

Loopback redirect on 127.0.0.1:8765; nothing leaves the machine except
the exchange with Google's token endpoint.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from core import envfile
from core.config import settings
from core.connectors.google_auth import TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PORT = 8765
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class ConsentError(Exception):
    """The consent flow did not produce a refresh token; the message says why."""


def consent(
    client_id: str,
    client_secret: str,
    *,
    open_browser: bool = True,
    timeout_s: float = 600,
) -> tuple[str, str]:
    """Run the loopback consent flow. Returns (refresh_token, scopes granted)."""
    scopes = list(SCOPES)
    redirect = f"http://127.0.0.1:{PORT}/"
    state_token = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state_token,
        }
    )

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("state", [""])[0] != state_token:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch; run the script again")
                return
            if "code" in qs:
                result["code"] = qs["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Atlas has what it needs. You can close this tab.")
            else:
                result["error"] = qs.get("error", ["unknown"])[0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"consent failed; see the terminal")

        def log_message(self, *a) -> None:  # quiet
            pass

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        raise ConsentError(f"port {PORT} is busy ({exc}); close the other consent window and retry") from exc
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this URL and approve access:\n\n" + url + "\n")
    if open_browser:
        webbrowser.open(url)
    thread.join(timeout=timeout_s)
    server.server_close()

    if "code" not in result:
        raise ConsentError(f"no authorization code received ({result.get('error', 'timeout')})")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise ConsentError(f"token exchange failed: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    refresh = data.get("refresh_token")
    if not refresh:
        raise ConsentError(
            "Google returned no refresh token. Remove Atlas at myaccount.google.com/permissions and run again."
        )
    return refresh, data.get("scope", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    parser.add_argument("--write-env", action="store_true", help="save the refresh token into .env")
    args = parser.parse_args()

    # Read .env fresh: the connect wizard may have just written the client.
    client_id = envfile.get("ATLAS_GOOGLE_CLIENT_ID") or settings.google_client_id
    client_secret = envfile.get("ATLAS_GOOGLE_CLIENT_SECRET") or settings.google_client_secret
    if not client_id or not client_secret:
        print("Set ATLAS_GOOGLE_CLIENT_ID and ATLAS_GOOGLE_CLIENT_SECRET in .env first.", file=sys.stderr)
        return 2

    try:
        refresh, scope = consent(
            client_id, client_secret, open_browser=not args.no_browser
        )
    except ConsentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.write_env:
        envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", refresh)
        print("Saved ATLAS_GOOGLE_REFRESH_TOKEN to .env. Restart the worker (or run deploy) to use it.")
    else:
        print("Paste this into .env:\n")
        print(f"ATLAS_GOOGLE_REFRESH_TOKEN={refresh}")
    print("\nScopes granted:", scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
