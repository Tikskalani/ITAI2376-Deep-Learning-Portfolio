"""Connect Atlas to Gmail, Google Calendar, Canvas and GitHub.

Run on the laptop from the repo root (scripts\\connect.bat does this):

  python -m scripts.connect            walk through whatever is not connected
  python -m scripts.connect --check    test what is in .env, change nothing
  python -m scripts.connect --redo canvas,github
                                       set these up again even if they work

Each step opens the page where the token is made, reads the token without
echoing it, proves it works against the provider's API, and only then writes
it to .env. Nothing is printed back and nothing leaves the laptop except the
check against the provider itself.

Exit codes: 0 nothing changed, 3 .env changed (restart Atlas to use it),
1 a check failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
import webbrowser
from dataclasses import dataclass

import httpx

from core import envfile
from core.connectors.google_auth import TOKEN_URL

CANVAS_DEFAULT = "https://hccs.instructure.com"
GITHUB_API = "https://api.github.com"
GITHUB_NEW_TOKEN = "https://github.com/settings/personal-access-tokens/new"
GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
CALENDAR_PRIMARY = "https://www.googleapis.com/calendar/v3/calendars/primary"
SOURCES = ("google", "canvas", "github", "alarm")
HEALTHCHECKS_SIGNUP = "https://healthchecks.io/accounts/signup/"
CHANGED = 3
_TIMEOUT = httpx.Timeout(20.0)


@dataclass
class Result:
    name: str
    ok: bool | None  # None: not configured
    detail: str


# ------------------------------------------------------------------ checks


def _client(client: httpx.Client | None):
    """The caller's client left open, or a fresh one closed after use."""
    return contextlib.nullcontext(client) if client is not None else httpx.Client(timeout=_TIMEOUT)


def _get_json(client: httpx.Client, url: str, **kw) -> tuple[int, dict]:
    try:
        resp = client.get(url, **kw)
    except httpx.HTTPError as exc:
        return 0, {"error": str(exc)}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {"items": body}


def check_canvas(url: str, token: str, client: httpx.Client | None = None) -> Result:
    if not (url and token):
        return Result("Canvas", None, "not connected")
    with _client(client) as c:
        status, body = _get_json(
            c, url.rstrip("/") + "/api/v1/users/self", headers={"Authorization": f"Bearer {token}"}
        )
    if status == 200:
        return Result("Canvas", True, f"signed in as {body.get('name') or body.get('short_name') or 'you'}")
    if status == 401:
        return Result("Canvas", False, "token refused (expired or deleted); make a new one")
    if status == 0:
        return Result("Canvas", False, f"could not reach {url}: {body.get('error')}")
    return Result("Canvas", False, f"HTTP {status} from {url}")


def check_github(token: str, client: httpx.Client | None = None) -> Result:
    if not token:
        return Result("GitHub", None, "not connected")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with _client(client) as c:
        status, body = _get_json(c, GITHUB_API + "/user", headers=headers)
    if status == 200:
        return Result("GitHub", True, f"signed in as {body.get('login', '?')}")
    if status == 401:
        return Result("GitHub", False, "token refused (expired or revoked); make a new one")
    if status == 0:
        return Result("GitHub", False, f"could not reach GitHub: {body.get('error')}")
    return Result("GitHub", False, f"HTTP {status} from GitHub")


def github_repos(token: str, client: httpx.Client | None = None, limit: int = 15) -> list[str]:
    """Repos the token can see, most recently pushed first."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with _client(client) as c:
        status, body = _get_json(
            c, GITHUB_API + "/user/repos", headers=headers, params={"per_page": 100, "sort": "pushed"}
        )
    if status != 200:
        return []
    return [r["full_name"] for r in body.get("items", []) if r.get("full_name")][:limit]


def check_google(
    client_id: str, client_secret: str, refresh: str, client: httpx.Client | None = None
) -> Result:
    if not (client_id and client_secret and refresh):
        missing = "client" if not (client_id and client_secret) else "consent"
        return Result("Google", None, f"not connected (needs the OAuth {missing})")
    with _client(client) as c:
        try:
            resp = c.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            return Result("Google", False, f"could not reach Google: {exc}")
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", "")
            except ValueError:
                err = ""
            if err == "invalid_grant":
                return Result("Google", False, "consent expired or revoked; run the Google step again")
            if err == "invalid_client":
                return Result("Google", False, "client id or secret is wrong")
            return Result("Google", False, f"token endpoint HTTP {resp.status_code} {err}".strip())
        auth = {"Authorization": f"Bearer {resp.json().get('access_token', '')}"}
        g_status, profile = _get_json(c, GMAIL_PROFILE, headers=auth)
        c_status, cal = _get_json(c, CALENDAR_PRIMARY, headers=auth)
    if g_status != 200:
        hint = " (is the Gmail API enabled?)" if g_status == 403 else ""
        return Result("Google", False, f"Gmail answered HTTP {g_status}{hint}")
    detail = f"Gmail {profile.get('emailAddress', '?')}"
    if c_status == 200:
        detail += f", Calendar {cal.get('summary') or 'primary'}"
    else:
        hint = " (is the Calendar API enabled?)" if c_status == 403 else ""
        return Result("Google", False, f"{detail} works; Calendar answered HTTP {c_status}{hint}")
    return Result("Google", True, detail)


def check_alarm(url: str, client: httpx.Client | None = None) -> Result:
    """The outside dead-man alarm (healthchecks.io or any ping URL). The
    check is itself a ping, which is harmless: it says Atlas is alive."""
    if not url:
        return Result("Alarm", None, "no outside alarm (nothing notices if the laptop is off)")
    if not url.startswith("https://"):
        return Result("Alarm", False, "the ping URL must start with https://")
    with _client(client) as c:
        try:
            resp = c.get(url)
        except httpx.HTTPError as exc:
            return Result("Alarm", False, f"could not reach {httpx.URL(url).host}: {exc}")
    if 200 <= resp.status_code < 300:
        return Result("Alarm", True, f"pinging {httpx.URL(url).host}; it alerts you if Atlas goes quiet")
    return Result("Alarm", False, f"HTTP {resp.status_code} from the ping URL; check it was copied whole")


def check_all(env: dict[str, str]) -> dict[str, Result]:
    return {
        "google": check_google(
            env.get("ATLAS_GOOGLE_CLIENT_ID", ""),
            env.get("ATLAS_GOOGLE_CLIENT_SECRET", ""),
            env.get("ATLAS_GOOGLE_REFRESH_TOKEN", ""),
        ),
        "canvas": check_canvas(env.get("ATLAS_CANVAS_URL", ""), env.get("ATLAS_CANVAS_TOKEN", "")),
        "github": check_github(env.get("ATLAS_GITHUB_TOKEN", "")),
        "alarm": check_alarm(env.get("ATLAS_HEARTBEAT_URL", "")),
    }


def print_results(results: dict[str, Result]) -> None:
    for r in results.values():
        mark = {True: "ok ", False: "!! ", None: "-- "}[r.ok]
        print(f"  {mark} {r.name:<7} {r.detail}")


# ------------------------------------------------------------------ prompts


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt} (paste; it will not show): ").strip()


def _open(url: str) -> None:
    print(f"    opening {url}")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - no browser just means "open it yourself"
        pass


def _pause(text: str = "Press Enter when done") -> None:
    input(f"    {text}... ")


# ------------------------------------------------------------------ setup steps


def setup_canvas(env: dict[str, str]) -> bool:
    print("\nCanvas (HCC). Atlas reads assignments, due dates, grades and announcements.")
    url = _ask("  Canvas address", env.get("ATLAS_CANVAS_URL") or CANVAS_DEFAULT).rstrip("/")
    print("  In Canvas: Account, Settings, scroll to Approved Integrations, + New Access Token.")
    print("  Purpose: Atlas. Leave the expiry blank (or pick the end of the semester).")
    _open(url + "/profile/settings")
    for _ in range(3):
        token = _secret("  Canvas token")
        if not token:
            print("  Skipped Canvas.")
            return False
        result = check_canvas(url, token)
        print_results({"canvas": result})
        if result.ok:
            envfile.set_value("ATLAS_CANVAS_URL", url)
            envfile.set_value("ATLAS_CANVAS_TOKEN", token)
            return True
    print("  Canvas not saved after three tries.")
    return False


def setup_github(env: dict[str, str]) -> bool:
    print("\nGitHub. Atlas watches pushes, PRs, issues and failed CI on your client repos.")
    print("  Make a fine-grained token: Repository access 'All repositories' (or pick the client")
    print("  repos), permissions read-only: Actions, Contents, Issues, Metadata, Pull requests.")
    print("  Expiry: one year. Atlas never writes to GitHub with it.")
    _open(GITHUB_NEW_TOKEN)
    for _ in range(3):
        token = _secret("  GitHub token")
        if not token:
            print("  Skipped GitHub.")
            return False
        result = check_github(token)
        print_results({"github": result})
        if result.ok:
            envfile.set_value("ATLAS_GITHUB_TOKEN", token)
            repos = github_repos(token)
            if repos:
                print("  Repos this token sees, newest push first:")
                for name in repos:
                    print(f"    {name}")
            print("  Projects already link their own repos. Add any extra repos to watch,")
            print("  comma separated (Enter keeps the current list).")
            extra = _ask("  Extra repos", env.get("ATLAS_GITHUB_REPOS", ""))
            if extra != env.get("ATLAS_GITHUB_REPOS", ""):
                envfile.set_value("ATLAS_GITHUB_REPOS", extra.replace(" ", ""))
            return True
    print("  GitHub not saved after three tries.")
    return False


GOOGLE_STEPS = [
    ("Create a Google Cloud project named Atlas (any name works).",
     "https://console.cloud.google.com/projectcreate"),
    ("Enable the Gmail API (check the project picker at the top says Atlas).",
     "https://console.cloud.google.com/apis/library/gmail.googleapis.com"),
    ("Enable the Google Calendar API.",
     "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"),
    ("Google Auth Platform, Get started: app name Atlas, your Gmail as support and contact\n"
     "    email, Audience External. Then Audience, Publish app, Confirm. Publishing matters:\n"
     "    in Testing mode Google expires the consent every 7 days. Google's 'unverified\n"
     "    app' warning is expected for a personal app; nobody else will see it.",
     "https://console.cloud.google.com/auth/overview"),
    ("Clients, Create client, type Desktop app, name Atlas laptop, Create.\n"
     "    Keep that window open; the next prompts ask for its client ID and secret.",
     "https://console.cloud.google.com/auth/clients/create"),
]


def setup_google(env: dict[str, str]) -> bool:
    # Imported here: it pulls in the http.server loopback only when needed.
    from scripts.google_auth import ConsentError, consent

    print("\nGoogle (Gmail and Calendar). Atlas reads mail and your calendar; drafts and")
    print("calendar holds always wait for your approval. About ten minutes, once.")
    have_client = bool(env.get("ATLAS_GOOGLE_CLIENT_ID") and env.get("ATLAS_GOOGLE_CLIENT_SECRET"))
    if have_client and _ask("  An OAuth client is already in .env. Reuse it? (y/n)", "y").lower().startswith("y"):
        client_id = env["ATLAS_GOOGLE_CLIENT_ID"]
        client_secret = env["ATLAS_GOOGLE_CLIENT_SECRET"]
    else:
        for i, (text, url) in enumerate(GOOGLE_STEPS, 1):
            print(f"\n  {i}. {text}")
            _open(url)
            _pause()
        client_id = _ask("\n  Client ID (ends in .apps.googleusercontent.com)")
        client_secret = _secret("  Client secret")
        if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
            print("  That does not look like a Desktop client ID and secret. Skipped Google.")
            return False
        envfile.set_value("ATLAS_GOOGLE_CLIENT_ID", client_id)
        envfile.set_value("ATLAS_GOOGLE_CLIENT_SECRET", client_secret)

    print("\n  A Google page opens next. Pick your account, then Advanced, Go to Atlas")
    print("  (the unverified warning), tick every box, Continue.")
    try:
        refresh, _scope = consent(client_id, client_secret)
    except ConsentError as exc:
        print(f"  Google consent failed: {exc}")
        return False
    result = check_google(client_id, client_secret, refresh)
    print_results({"google": result})
    if not result.ok:
        print("  Not saved. Fix the item above and run the Google step again (--redo google).")
        return False
    envfile.set_value("ATLAS_GOOGLE_REFRESH_TOKEN", refresh)
    return True


def setup_alarm(env: dict[str, str]) -> bool:
    print("\nOutside alarm. Atlas pings healthchecks.io after every loop tick (every 30 minutes).")
    print("When the pings stop (laptop asleep, off, offline, or Atlas stuck), healthchecks.io")
    print("emails you, and its app can push to your phone. Free; nothing but the ping is sent.")
    print("  1. Sign up (free). 2. Add Check: name Atlas, Period 30 minutes, Grace 30 minutes.")
    print("  3. Copy the ping URL (https://hc-ping.com/...).")
    _open(HEALTHCHECKS_SIGNUP)
    for _ in range(3):
        url = _ask("  Ping URL").strip()
        if not url:
            print("  Skipped the alarm.")
            return False
        result = check_alarm(url)
        print_results({"alarm": result})
        if result.ok:
            envfile.set_value("ATLAS_HEARTBEAT_URL", url)
            return True
    print("  Alarm not saved after three tries.")
    return False


SETUP = {"google": setup_google, "canvas": setup_canvas, "github": setup_github, "alarm": setup_alarm}


# ------------------------------------------------------------------ main


def _parse_sources(text: str) -> list[str]:
    names = [s.strip().lower() for s in text.split(",") if s.strip()]
    bad = [n for n in names if n not in SOURCES]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown source(s): {', '.join(bad)}; use {', '.join(SOURCES)}")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect Atlas to Gmail, Calendar, Canvas, GitHub and an outside alarm.")
    parser.add_argument("--check", action="store_true", help="test what is in .env and change nothing")
    parser.add_argument("--redo", type=_parse_sources, default=[], help="comma list to set up again")
    parser.add_argument("--only", type=_parse_sources, default=[], help="comma list to consider")
    args = parser.parse_args(argv)

    if not envfile.PATH.exists():
        print(".env is missing. Run scripts\\deploy.bat once first; it creates it.", file=sys.stderr)
        return 2

    env = envfile.read()
    print("Atlas connections:")
    results = check_all(env)
    print_results(results)
    if args.check:
        return 1 if any(r.ok is False for r in results.values()) else 0

    wanted = args.only or list(SOURCES)
    todo = [s for s in wanted if s in args.redo or not results[s].ok]
    if not todo:
        print("\nEverything is connected. Use --redo <name> to replace a token.")
        return 0

    changed = False
    for name in todo:
        if _ask(f"\nSet up {results[name].name} now? (y/n)", "y").lower().startswith("y"):
            changed = SETUP[name](envfile.read()) or changed

    print("\nAtlas connections now:")
    final = check_all(envfile.read())
    print_results(final)
    if changed:
        print("\nSaved to .env. Atlas picks these up when its services restart.")
        return CHANGED
    return 1 if any(final[s].ok is False for s in todo) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Anything already checked and saved stays saved.")
        sys.exit(1)
