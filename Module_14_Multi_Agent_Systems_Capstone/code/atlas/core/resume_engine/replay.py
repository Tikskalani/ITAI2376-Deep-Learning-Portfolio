"""Recorded model replies, played back so a regression run costs nothing.

recorder() wraps a complete function and appends every call to a JSONL file:
the stage, the prompts, the reply, and a key (a SHA-256 of stage, system prompt
and user prompt). Replay reads that file and answers each call by its key, with
no network and no spend. A call whose key was never recorded is a miss: Replay
records it and gives no answer, which the bridge treats as a failed call (one
fallback try, then the run stops), so a prompt that changed cannot pass quietly.

Recordings hold the resume and posting text, so they live under data/ (gitignored)
and are never committed. Tests that replay a real recording skip where none exists.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.resume_engine.bridge import PURPOSE_PREFIX

CompleteFn = Callable[..., Coroutine[Any, Any, str]]


def key(stage: str, system: str, user: str) -> str:
    return hashlib.sha256(json.dumps([stage, system, user], ensure_ascii=False).encode("utf-8")).hexdigest()


def _stage(purpose: str) -> str:
    return (purpose or "").removeprefix(PURPOSE_PREFIX)


def recorder(complete: CompleteFn, path: Path) -> CompleteFn:
    """complete, writing every call and its reply to `path` (JSONL)."""

    async def record(**kw: Any) -> str:
        text = await complete(**kw)
        stage = _stage(kw.get("purpose", ""))
        line = {"key": key(stage, kw.get("system", ""), kw.get("user", "")), "stage": stage,
                "purpose": kw.get("purpose"), "tier": kw.get("tier"), "system": kw.get("system"),
                "user": kw.get("user"), "reply": text}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return text

    return record


class Replay:
    """A complete function answering from a recording. Thread-safe enough for the
    bridge: calls arrive on the event loop one at a time."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._replies: dict[str, deque[str]] = {}
        self._last: dict[str, str] = {}
        for e in entries:
            k = e.get("key") or key(e.get("stage") or _stage(e.get("purpose", "")), e["system"], e["user"])
            self._replies.setdefault(k, deque()).append(e["reply"])
        self.hits = 0
        self.misses: list[str] = []

    @classmethod
    def from_jsonl(cls, path: Path) -> Replay:
        with path.open(encoding="utf-8") as f:
            return cls([json.loads(line) for line in f if line.strip()])

    async def __call__(self, *, system: str, user: str, purpose: str, on_usage: Any = None,
                       tier: str = "scan", **_: Any) -> str:
        stage = _stage(purpose)
        k = key(stage, system, user)
        queue = self._replies.get(k)
        if queue:
            text = queue.popleft()
            self._last[k] = text
        elif k in self._last:
            text = self._last[k]  # the same prompt asked again: the same answer
        else:
            self.misses.append(stage)
            return ""  # no on_usage: the bridge sees a failed call
        self.hits += 1
        if on_usage is not None:
            on_usage(f"replay/{tier}", {}, Decimal("0"))
        return text


__all__ = ["Replay", "key", "recorder"]
