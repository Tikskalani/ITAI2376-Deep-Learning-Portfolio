"""Atlas -- the setup checklist: the one-time chores, with a live status each.

Connect Google, Canvas and GitHub, subscribe a phone to push, install the
submitter's browser. These used to be tasks the CEO put on the Plan, where
they went overdue and stayed red; they are not tasks with a date, they are
setup with a state Atlas can read. Settings, Connections shows this list at the
top and each item turns green on its own once the thing works.

Statuses come from what Atlas already knows (.env, connector_state,
push_subscriptions, the submitter's preflight). Nothing here calls a
provider or a model; the browser check launches Chromium locally, cached.

Agents must not put these chores back on the Plan: chore_for() recognises
one from a task title and agents/ceo/tools.py refuses it with a pointer here.
"""

from __future__ import annotations

import re
from typing import Any

from core import db
from core.config import settings

# Item id -> patterns that mean "this task is that chore".
_CHORES: dict[str, re.Pattern[str]] = {
    "google": re.compile(r"connect\.bat|connect (gmail|google|calendar)|google (oauth|consent|connect)", re.I),
    "canvas": re.compile(r"connect canvas|canvas (token|connect)", re.I),
    "github": re.compile(r"connect github|github (token|connect)|personal access token", re.I),
    "push": re.compile(r"(subscribe|enable|turn on).{0,30}push|push (notification|subscription)", re.I),
    "submitter": re.compile(r"playwright|submitter browser|install chromium|headless shell", re.I),
}


def chore_for(title: str) -> str | None:
    """The checklist item a task title is about, or None for a real task."""
    for item, pattern in _CHORES.items():
        if pattern.search(title or ""):
            return item
    return None


def _phone_url() -> str | None:
    """The address the phone opens Atlas at (the tunnel writes it here)."""
    path = settings.log_dir / "public_url.txt"
    try:
        url = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return url if url.startswith("https://") else None


def _connector(block: dict[str, Any], name: str, steps: list[str]) -> dict[str, Any]:
    ok = block.get("ok") is True and not block.get("needs_reconnect")
    return {"id": name.lower(), "label": f"Connect {name}", "done": ok,
            "detail": str(block.get("detail") or ("connected" if ok else "not connected")),
            "steps": [] if ok else steps, "action": {"kind": "tile", "tile": name.lower()}}


async def checklist() -> dict[str, Any]:
    """[{id, label, done, detail, steps, action}] in the order to do them."""
    from core import connect
    from core.jobs.submit.browser import preflight
    from core.surfaces.notify import push_configured

    status = await connect.status(check=False)
    items = [
        _connector(status["google"], "Google", [
            "In this tile, follow the three Google Cloud steps if no client is saved yet, paste the id and secret, click Save.",
            "Click Connect Google, sign in as yourself, click Continue past the unverified-app screen, tick both boxes "
            "(read Gmail, read the calendar: Atlas asks for read-only access), click Allow.",
            "Before connecting, list the client senders, domains and labels no model may read in "
            "data/model_exclude.yaml (docs/privacy.md). Safe-start keeps all mail from models until you turn it on.",
            "Do this from the laptop's own browser (127.0.0.1): Google only returns there.",
        ]),
        _connector(status["canvas"], "Canvas", [
            "Click Open Canvas settings below, then New access token, name it Atlas, click Generate token.",
            "Copy the token, paste it below, click Connect.",
        ]),
        _connector(status["github"], "GitHub", [
            "Open the fine-grained token link above, pick your repos, give Contents and Issues read-only, click Generate token.",
            "Paste the token below, click Connect.",
        ]),
        # The only alert that reaches him when the laptop itself is off or asleep.
        {**_connector(status.get("alarm") or {}, "Outside alarm", [
            "Make a free check at healthchecks.io (period 30 minutes, grace 30 minutes) and copy its ping URL.",
            "Paste the URL below, click Connect. The loop pings it every tick.",
        ]), "id": "alarm", "label": "Set the outside alarm", "action": {"kind": "tile", "tile": "alarm"}},
    ]

    devices = int(await db.fetchval("select count(*) from push_subscriptions") or 0)
    url = _phone_url()
    if not push_configured():
        push_detail = "Push is not configured on the server (ATLAS_VAPID_* in .env)."
        push_steps = ["Run scripts\\deploy.bat (one UAC click); it writes the missing push keys and restarts Atlas."]
    elif devices:
        push_detail = f"{devices} device{'s' if devices != 1 else ''} subscribed; alerts reach the phone."
        push_steps = []
    else:
        push_detail = "No device is subscribed, so alerts wait in the inbox."
        push_steps = [
            f"On the iPhone, open {url} in Safari." if url else "On the iPhone, open Atlas's address in Safari (see docs/atlas-local-host.md).",
            "Tap Share, then Add to Home Screen, then Add. Open Atlas from the new icon (push only works there).",
            "Open Settings, Connections there and tap Subscribe this device, then Allow. Tap Send a test push to check.",
        ]
    items.append({"id": "push", "label": "Subscribe your phone to push", "done": devices > 0,
                  "detail": push_detail, "steps": push_steps, "action": {"kind": "push"}, "devices": devices})

    browser = await preflight()
    items.append({"id": "submitter", "label": "Install the submitter's browser", "done": browser["ok"],
                  "detail": browser["detail"],
                  "steps": [] if browser["ok"] else [
                      "Open a terminal in the Atlas folder as the same Windows user the AtlasWorker service runs as.",
                      "Run each command below, then click Check again.",
                  ],
                  "commands": browser["commands"],
                  "action": {"kind": "recheck"}})
    done = sum(1 for i in items if i["done"])
    return {"items": items, "done": done, "total": len(items), "complete": done == len(items)}


__all__ = ["checklist", "chore_for"]
