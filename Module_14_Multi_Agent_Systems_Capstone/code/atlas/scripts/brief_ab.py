"""Side-by-side: the same brief written by two CEO models, for Trilok to judge.

    python -m scripts.brief_ab --kind evening --cap 1.50

Builds one brief's inputs (the signals block and the prompt), then runs the
CEO on them twice: the configured CEO model (Opus) and, with only the model
changed, the specialist model (Sonnet). Same system prompt, tools, effort and
turn limit. Nothing is delivered and nothing is saved: tools that write or
dispatch (remember_fact, add_task, dispatch_agent, ...) answer "test run, not
saved" for both models alike, and the run's working-context note is skipped.
Both runs are recorded as episodes, so their cost shows in the spend.

The cap is checked before the second run: if the first run's cost plus the
second's projected cost would pass it, the second run does not start.

Writes data/brief_ab/<stamp>/briefs.md (the two briefs as A and B, order
shuffled) and key.json (which is which, tokens, dollars). Read briefs.md
first, decide, then open key.json.

This is a test of a tier change Trilok has not approved. It changes no setting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core import db
from core.config import REPO_ROOT, settings
from core.costs import cost_of_usage
from core.loop import briefs
from core.runtime import agent as rt
from core.runtime.dispatcher import _REGISTRY
from core.runtime.register_all import register_all

READ_ONLY = {"recall_facts", "recall_lessons", "list_goals", "plan_week", "explain_run"}
# Sonnet's share of an Opus run at the same tokens (input and output rates).
_SECOND_RUN_SHARE = Decimal("0.5")


def slot_day(kind: str, now: datetime | None = None) -> date:
    """The day whose brief is being simulated. An evening test run after
    midnight belongs to the evening before (2026-10-22 01:15 was labeled
    "the evening of Thursday, October 22" and both models misread it)."""
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.timezone))
    if kind == "evening" and local.time() < settings.brief_morning:
        return local.date() - timedelta(days=1)
    return local.date()


def _pinned(day: date) -> Any:
    """Pin the prompt's date line and the runtime's "Today is" line to `day`."""
    class _Day(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return day

    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(briefs, "_today_str", lambda: day.strftime("%A, %B %d, %Y")))
    stack.enter_context(patch.object(rt, "date", _Day))
    return stack


def _stub(name: str) -> rt.ToolHandler:
    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "note": f"test run: {name} was not carried out or saved"}
    return handler


def _ceo_config() -> rt.AgentConfig:
    register_all()
    reg = _REGISTRY["ceo"]
    tools = [t if t.name in READ_ONLY else rt.ToolSpec(t.name, t.description, t.input_schema, _stub(t.name))
             for t in (reg.tools_factory() if reg.tools_factory else [])]
    return rt.load_agent_config("ceo", reg.model_tier, tools, **reg.overrides)


async def _run(model: str, prompt: str, context: str) -> dict[str, Any]:
    cfg = _ceo_config()

    async def no_note(*_a: Any, **_k: Any) -> None:
        return None

    with (
        patch.object(settings, "model_ceo", model),
        patch("core.agent_context.record_context", new=no_note),
        patch.object(rt, "_record_playbook_uses", new=no_note),
    ):
        result = await rt.run_agent(cfg, prompt, participants=["owner", "ceo", "brief-ab"], memory_context=context)
    usage = dict(result.usage or {})
    return {"model": model, "text": (result.text or "").strip(), "usage": usage, "turns": result.turns,
            "usd": str(cost_of_usage(model, usage))}


async def main(kind: str, cap: Decimal) -> None:
    day = slot_day(kind)
    with _pinned(day):
        await _main(kind, cap, day)


async def _main(kind: str, cap: Decimal, day: date) -> None:
    context = await briefs._signals_context(kind)
    prompt = briefs._brief_prompt(kind)
    print(f"simulating the {kind} brief of {day:%a %b %d}")
    first = await _run(settings.model_ceo, prompt, context)
    spent = Decimal(first["usd"])
    print(f"{first['model']}: ${spent} ({first['turns']} turns); cap ${cap}")
    runs = [first]
    projected = spent * _SECOND_RUN_SHARE
    if spent + projected > cap:
        print(f"stopping: the second run would take the test to about ${spent + projected}, past the ${cap} cap")
    else:
        second = await _run(settings.model_specialist, prompt, context)
        spent += Decimal(second["usd"])
        runs.append(second)
        print(f"{second['model']}: ${second['usd']} ({second['turns']} turns); total ${spent}")
    out = REPO_ROOT / "data" / "brief_ab" / datetime.now(UTC).strftime("%Y%m%dT%H%M")
    out.mkdir(parents=True, exist_ok=True)
    order = runs[:]
    random.shuffle(order)
    labels = dict(zip("AB", order, strict=False))
    (out / "briefs.md").write_text(
        f"# {kind.capitalize()} brief, side by side\n\nSame inputs, two models. Decide before opening key.json.\n\n"
        + "\n\n".join(f"## {k}\n\n{r['text']}" for k, r in labels.items()) + "\n",
        encoding="utf-8",
    )
    (out / "key.json").write_text(json.dumps(
        {k: {key: r[key] for key in ("model", "usd", "turns", "usage")} for k, r in labels.items()}
        | {"total_usd": str(spent), "cap_usd": str(cap), "kind": kind}, indent=2), encoding="utf-8")
    print(f"written to {out}")
    await db.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=("morning", "evening"), default="evening")
    parser.add_argument("--cap", type=Decimal, default=Decimal("1.50"))
    args = parser.parse_args()
    asyncio.run(main(args.kind, args.cap))
