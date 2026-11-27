"""Atlas -- the approval policy.

One table, `approval_policy`, decides what the runtime may do with a tool
call before it runs: `auto` (just run it), `approve` (raise an approval
card and run only on yes), or `deny` (never run it, tell the model why).
Patterns are fnmatch-style over the tool name, so `*send_*` covers
`gmail_send_message` and `send_sms` alike.

Why a table and not per-agent code: the hard rules in AGENTS.md (money,
sent messages, public posts, submitted forms, account changes) are the
same for every agent, they should be auditable in one place, and Trilok
should be able to tighten or loosen one without a deploy.

Resolution when several patterns match: deny beats approve beats auto,
and within the same gate the longest pattern is the one recorded as the
reason.

Fail closed. A tool no row matches is "unmatched" and is refused, with an
event saying which row to add: every tool Atlas ships has an explicit row
(migration 037, and tests/test_tool_policy_coverage.py keeps it that way),
so an unmatched name is a new tool nobody has classified yet. A policy
table that cannot be read is "unavailable" and refuses too: a lost
database must not quietly grant execution. Reads are cached for a minute,
so an outage shorter than that still answers from the last good read.

What this covers: every ToolSpec call through the runtime loop
(core/runtime/agent.py) and late approvals (core/runtime/late.py), and
whether an agent is given its Anthropic-hosted server tools at all
(pattern "server:<name>"). It does not see individual server-tool or
remote MCP calls (Anthropic runs them), the Atlas MCP server's own tools
(core/mcp_server.py, called by Trilok's clients), or code paths that act
without a tool call (the job submitter, workflow notify steps).
"""

from __future__ import annotations

import logging
import time
from fnmatch import fnmatchcase
from typing import Any

from core import db

log = logging.getLogger(__name__)

GATES = ("auto", "approve", "deny")
# Two more outcomes gate_for can return; both refuse. Never stored in the table.
UNMATCHED = "unmatched"
UNAVAILABLE = "unavailable"
REFUSING = (UNMATCHED, UNAVAILABLE, "deny")
_RANK = {"deny": 2, "approve": 1, "auto": 0}
_TTL_SECONDS = 60.0

_cache: tuple[float, list[dict[str, Any]]] | None = None


def match_gate(tool_name: str, policies: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Pure resolution: (gate, matching pattern), or (UNMATCHED, None)."""
    name = (tool_name or "").lower()
    best: tuple[int, int, str, str] | None = None
    for p in policies:
        pattern = str(p.get("tool_pattern") or "").lower()
        gate = str(p.get("gate") or "auto")
        if not pattern or gate not in _RANK:
            continue
        if fnmatchcase(name, pattern):
            key = (_RANK[gate], len(pattern), gate, pattern)
            if best is None or key > best:
                best = key
    if best is None:
        return UNMATCHED, None
    return best[2], best[3]


async def list_policies(*, force: bool = False) -> list[dict[str, Any]]:
    """All rows, cached for a minute. force=True bypasses the cache."""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    rows = await db.fetch(
        "select id, tool_pattern, gate, note, created_at from approval_policy "
        "order by tool_pattern"
    )
    policies = [dict(r) for r in rows]
    _cache = (now, policies)
    return policies


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def gate_for(tool_name: str) -> tuple[str, str | None]:
    """The gate for a tool call. UNAVAILABLE if the table cannot be read
    (the caller refuses), UNMATCHED if no row matches (refused too)."""
    try:
        policies = await list_policies()
    except Exception as exc:
        log.exception("approval policy unreadable; refusing %s", tool_name)
        return UNAVAILABLE, f"{type(exc).__name__}: {exc}"[:200]
    return match_gate(tool_name, policies)


def refusal_message(tool_name: str, gate: str, pattern: str | None) -> str:
    """What the model (and the audit row) is told when a gate refuses."""
    if gate == UNMATCHED:
        return (f"Tool {tool_name} has no Atlas policy, so it did not run. Trilok has been told; "
                "carry on without it and say what you would have done.")
    if gate == UNAVAILABLE:
        return (f"Tool {tool_name} did not run: Atlas could not read its tool policy ({pattern}). "
                "Nothing was done; report that and stop using tools that change anything.")
    return (f"Tool {tool_name} is blocked by Atlas policy ({pattern}). "
            "Prepare the content for Trilok to act on himself instead.")


async def announce_refusal(tool_name: str, gate: str, pattern: str | None, *,
                           agent: str | None = None, episode_id: Any = None) -> None:
    """Tell Trilok, on the event stream, what to fix. Only for the two
    refusals that are a gap in the policy rather than a rule working."""
    if gate not in (UNMATCHED, UNAVAILABLE):
        return
    from core import events

    if gate == UNMATCHED:
        title = f"{tool_name} has no policy row, so it was refused"
        detail = (f"Classify it on Inbox > Trust (auto, approve or deny), or add an "
                  f"approval_policy row for '{tool_name}'.")
    else:
        title = f"Tool policy unreadable; {tool_name} was refused"
        detail = f"Check the database: {pattern}"
    await events.emit("policy.refused", agent=agent, episode_id=episode_id, title=title,
                      detail=detail, data={"tool": tool_name, "gate": gate})


async def set_policy(tool_pattern: str, gate: str, note: str | None = None) -> None:
    if gate not in GATES:
        raise ValueError(f"gate must be one of {GATES}, got {gate!r}")
    pattern = tool_pattern.strip()
    if not pattern:
        raise ValueError("tool_pattern must not be empty")
    await db.execute(
        """
        insert into approval_policy (tool_pattern, gate, note)
        values ($1, $2, $3)
        on conflict (tool_pattern) do update set gate = excluded.gate,
          note = coalesce(excluded.note, approval_policy.note)
        """,
        pattern,
        gate,
        note,
    )
    invalidate_cache()


async def delete_policy(tool_pattern: str) -> bool:
    res = await db.execute(
        "delete from approval_policy where tool_pattern = $1", tool_pattern.strip()
    )
    invalidate_cache()
    return res.endswith("1")


__all__ = [
    "GATES",
    "UNMATCHED",
    "UNAVAILABLE",
    "REFUSING",
    "refusal_message",
    "announce_refusal",
    "match_gate",
    "list_policies",
    "invalidate_cache",
    "gate_for",
    "set_policy",
    "delete_policy",
]
