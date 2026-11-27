"""Atlas -- the trust ladder, on screen.

The approval policy (core/runtime/policy.py) is the fence: per tool
pattern, never (deny), asks first (approve), or on its own (auto). It was
a table nobody saw. The ladder shows every rung with what the tools
behind it have earned: calls in the window, errors, cards asked and how he
answered, when it last ran. Tools that match no pattern run on their own
by default; they are listed too, so one tap fences them.

Moving a tool up the ladder is his decision and his alone. Nothing here
promotes a tool from a track record; it only shows the record next to
the switch, which is what the vision calls "permissions earned on a
visible track record".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from core import db
from core.runtime import policy

RUNGS = {"deny": "never", "approve": "asks first", "auto": "on its own",
         policy.UNMATCHED: "refused: no policy yet"}
_ORDER = {"deny": 0, "approve": 1, "auto": 2, policy.UNMATCHED: 3}

# Money and account rules stay "never" (AGENTS.md hard rules). The screen
# cannot remove them or move them up; only a migration can.
LOCKED = frozenset({"*transfer*", "*payment*", "*close_account*", "*change_password*"})
# Sent messages and public posts always wait for his yes (AGENTS.md hard
# rules): "never" or "asks first", not "on its own". Submitting forms is his
# call (2026-10-22: he set *submit* to "on its own" so Apply applies).
NO_AUTO = frozenset({"*send_*", "*reply*", "*comment*", "*publish*", "*draft_email*", "*create_event*"})

# What each seeded pattern means, in his words; a pattern not listed shows its tool names.
LABELS = {
    "*transfer*": "Moving money", "*payment*": "Payments", "*close_account*": "Closing accounts",
    "*change_password*": "Changing passwords", "*submit*": "Submitting forms (agents)",
    "*publish*": "Publishing", "*send_*": "Sending messages", "*reply*": "Replies",
    "*comment*": "Comments", "*create_event*": "Calendar events", "*draft_email*": "Email drafts",
}


class LockedRule(ValueError):
    """A money or account rule: it stays "never"."""


def _note(note: str | None) -> str | None:
    """Notes without migration bookkeeping like "(privacy gate, 035)"."""
    return re.sub(r"\s*\([^)]*\b\d{3}\)", "", note).strip() or None if note else note


def _blank(pattern: str, gate: str, note: str | None, fenced: bool) -> dict[str, Any]:
    label = LABELS.get(pattern) or (pattern.strip("*").replace("_", " ").capitalize() if fenced else pattern)
    return {"pattern": pattern, "label": label, "locked": pattern in LOCKED, "no_auto": pattern in NO_AUTO,
            "gate": gate,
            "rung": RUNGS.get(gate, gate), "note": _note(note), "tools": [],
            "calls": 0, "errors": 0, "asked": 0, "approved": 0, "rejected": 0, "expired": 0,
            "last_at": None, "fenced": fenced}


async def ladder(days: int = 30) -> dict[str, Any]:
    """Every rung with its record. Unfenced tools come last, each its own row."""
    days = max(1, min(int(days), 365))
    since = datetime.now(UTC) - timedelta(days=days)
    policies = await policy.list_policies(force=True)
    used = await db.fetch(
        """
        select inputs->>'tool' as tool, count(*) as calls,
               count(*) filter (where outputs ? 'error') as errors, max(created_at) as last_at
        from actions
        where action_type = 'tool_call' and created_at >= $1 and inputs->>'tool' is not null
        group by 1
        """,
        since,
    )
    asked = await db.fetch(
        """
        select action_payload->>'tool' as tool, count(*) as asked,
               count(*) filter (where status = 'approved') as approved,
               count(*) filter (where status = 'rejected') as rejected,
               count(*) filter (where status = 'expired') as expired
        from approvals where action_payload ? 'tool' and created_at >= $1
        group by 1
        """,
        since,
    )
    used_by = {r["tool"]: r for r in used}
    asked_by: dict[str, dict[str, int]] = {r["tool"]: {k: int(r[k]) for k in ("asked", "approved", "rejected", "expired")}
                                           for r in asked}
    rows: dict[str, dict[str, Any]] = {p["tool_pattern"]: _blank(p["tool_pattern"], p["gate"], p.get("note"), True)
                                       for p in policies}
    for tool in sorted(set(used_by) | set(asked_by)):
        gate, pattern = policy.match_gate(tool, policies)
        row = rows[pattern] if pattern is not None else rows.setdefault(tool, _blank(tool, policy.UNMATCHED, None, False))
        row["tools"].append(tool)
        u = used_by.get(tool)
        if u is not None:
            row["calls"] += int(u["calls"])
            row["errors"] += int(u["errors"])
            if row["last_at"] is None or (u["last_at"] and u["last_at"] > row["last_at"]):
                row["last_at"] = u["last_at"]
        for k, v in asked_by.get(tool, {}).items():
            row[k] += v
    out = sorted(rows.values(), key=lambda r: (not r["fenced"], _ORDER.get(r["gate"], 9), -r["calls"], r["pattern"]))
    return {"days": days, "rungs": out, "counts": {g: sum(1 for r in out if r["gate"] == g) for g in _ORDER}}


async def set_gate(pattern: str, gate: str, note: str | None = None) -> dict[str, Any]:
    """His decision: move a pattern (or a bare tool name) to a rung. A locked
    money or account rule stays "never"."""
    if pattern.strip() in LOCKED and gate != "deny":
        raise LockedRule(f"{LABELS.get(pattern.strip(), pattern)} is never automated; it cannot be moved here.")
    if pattern.strip() in NO_AUTO and gate == "auto":
        raise LockedRule(f"{LABELS.get(pattern.strip(), pattern)} always waits for your yes; it can ask first or never run.")
    await policy.set_policy(pattern, gate, note)
    found = await ladder()
    return next((r for r in found["rungs"] if r["pattern"] == pattern.strip()),
                _blank(pattern.strip(), gate, note, True))


async def remove(pattern: str) -> bool:
    """Take the fence off a pattern. A tool no other row matches is then
    refused (unmatched), not run on its own. A locked money or account rule
    cannot be removed."""
    if pattern.strip() in LOCKED:
        raise LockedRule(f"{LABELS.get(pattern.strip(), pattern)} is never automated; it cannot be removed.")
    return await policy.delete_policy(pattern)


__all__ = ["LABELS", "LOCKED", "NO_AUTO", "LockedRule", "RUNGS", "ladder", "set_gate", "remove"]
