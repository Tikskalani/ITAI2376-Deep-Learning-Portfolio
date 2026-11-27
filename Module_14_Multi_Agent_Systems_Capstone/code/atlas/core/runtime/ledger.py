"""Atlas -- the tool ledger: an outward action never happens twice.

Every tool the approval policy gates (anything with hands: send, submit,
post, pay) goes through the ledger. A call is identified by the unit of
work it belongs to (its scope), the tool, and its exact input. If the same
call comes round again inside the same scope, which is what a retry is,
the ledger answers from its record instead of acting again:

  done      the earlier result is returned; nothing runs
  running   an earlier attempt may have acted and never reported back;
            refused, because a second send is worse than a missing one
  pending   an approval card for it is already open; no second card
  rejected  Trilok already said no; not asked again
  failed / expired   claimed again and tried normally

A handler that crashes is assumed to have acted (a send can time out after
it went through) unless it raises NotStarted.

Scopes nest outward: a workflow step sets one for all its retries, a
dispatch sets one for its own retry if no step has, and a bare agent run
falls back to its episode. Set them with `ensure_scope()`.

This is the fix the agent-framework research found most asked for (CrewAI
and LangGraph both re-fire side effects on retry).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core import db

log = logging.getLogger(__name__)

_scope: ContextVar[str | None] = ContextVar("atlas_action_scope", default=None)

RECLAIMABLE = ("failed", "expired")


class NotStarted(Exception):
    """Raised by a gated tool's handler when it failed before acting (bad
    input, a precondition not met). Only this makes a failed call safe to
    try again; any other exception counts as "may have acted"."""


def current_scope() -> str | None:
    return _scope.get()


@contextmanager
def ensure_scope(name: str) -> Iterator[str]:
    """Run the block inside `name` unless an outer scope is already set; the
    outermost unit of work owns the retries, so it wins."""
    outer = _scope.get()
    if outer is not None:
        yield outer
        return
    token = _scope.set(name)
    try:
        yield name
    finally:
        _scope.reset(token)


def call_key(scope: str, tool: str, tool_input: Any) -> str:
    """Stable identity of one call: key order and whitespace never matter."""
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{scope}\x1f{tool}\x1f{canonical}".encode()).hexdigest()


@dataclass
class Prior:
    """A call already in the ledger, which the caller must not repeat."""

    status: str
    result: Any

    def message(self, tool: str) -> str:
        return {
            "done": f"{tool} already ran for this exact input earlier in this task; it was not repeated.",
            "running": (
                f"An earlier attempt at {tool} with this exact input may have run and never reported back. "
                "It was not repeated, because doing it twice could be worse. Tell Trilok so he can check."
            ),
            "pending": f"An approval card for {tool} with this exact input is already open; no second card was raised.",
            "rejected": f"Trilok already rejected {tool} with this exact input; it was not asked again.",
        }.get(self.status, f"{tool} was not repeated ({self.status}).")


async def claim(
    key: str, *, scope: str, tool: str, agent_name: str, tool_input: Any, episode_id: UUID | None
) -> Prior | None:
    """Claim the call. None means go ahead; a Prior means do not act."""
    row = await db.fetchrow(
        """
        insert into tool_ledger (key, scope, tool, agent_name, input, episode_id)
        values ($1, $2, $3, $4, $5, $6)
        on conflict (key) do update
          set status = 'running', attempts = tool_ledger.attempts + 1,
              episode_id = excluded.episode_id, updated_at = now()
          where tool_ledger.status = any($7::text[])
        returning status
        """,
        key, scope, tool, agent_name, _jsonable(tool_input), episode_id, list(RECLAIMABLE),
    )
    if row is not None:
        return None  # inserted fresh, or reclaimed a failed/expired attempt
    prior = await db.fetchrow("select status, result from tool_ledger where key = $1", key)
    if prior is None:  # deleted between the two statements: treat as fresh
        return await claim(key, scope=scope, tool=tool, agent_name=agent_name,
                           tool_input=tool_input, episode_id=episode_id)
    log.info("ledger: %s in %s already %s; not repeated", tool, scope, prior["status"])
    return Prior(prior["status"], prior["result"])


async def settle(key: str, status: str, *, result: Any = None, approval_id: UUID | None = None) -> None:
    """Record how the claimed call ended."""
    await db.execute(
        "update tool_ledger set status = $2, result = $3, approval_id = coalesce($4, approval_id), "
        "updated_at = now() where key = $1",
        key, status, _jsonable(result) if result is not None else None, approval_id,
    )


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
