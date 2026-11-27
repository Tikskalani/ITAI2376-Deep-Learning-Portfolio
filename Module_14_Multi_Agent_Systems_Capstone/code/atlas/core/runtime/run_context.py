"""Which run a tool call belongs to, as the runtime knows it.

A tool handler is given only its arguments. A tool that must bind what it
writes to the run that made the call (core/capabilities/retrieval.py: an
outcome may only be reported for a version this run retrieved) reads the
run from here. The runtime sets it around each handler call
(core/runtime/agent.py, _run_tool_inner) and resets it after, so
concurrent runs, each in its own task, never see each other's identity.
Nothing a model writes in a tool's input is taken as the run's identity.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RunIdentity:
    episode_id: UUID
    agent: str
    project_slug: str | None = None


_current: ContextVar[RunIdentity | None] = ContextVar("atlas_run_identity", default=None)


def current() -> RunIdentity | None:
    """The run the calling tool belongs to, or None outside a run."""
    return _current.get()


@contextmanager
def bound(identity: RunIdentity) -> Iterator[RunIdentity]:
    token = _current.set(identity)
    try:
        yield identity
    finally:
        _current.reset(token)


__all__ = ["RunIdentity", "bound", "current"]
