"""The APEX engine's model, made of Atlas's llm.complete, under a hard USD cap.

The engine is synchronous and fans out up to six model calls at once from its
own threads (apex_engine._pmap). It calls `llm(system, user, *, max_tokens,
json_mode, effort)` and expects text back or an exception. EngineLLM is that
callable: it names the stage from the system prompt, routes the stage to a tier
from config/resume_engine.yaml, and runs llm.complete on the event loop the run
started from (the engine itself runs in a worker thread).

Money. Before every paid call, EngineLLM reserves the call's worst case under
one lock: every input byte as a token (a token is never shorter than a byte)
plus the full output budget, priced from costs.MODEL_PRICES. A call that could
push spent + reserved past the cap waits for calls in flight to settle; a call
that could not fit even after they settle stops the run. When a call returns,
its reservation is replaced by its actual cost. So spent + reserved never
passes the cap, and spend never does, however the six threads interleave. The
cap of one run is the smallest of: ATLAS_RESUME_RUN_USD_CAP (or the caller's),
what ATLAS_RESUME_DAILY_USD_CAP leaves today, and what ATLAS_DAILY_USD_CAP
leaves today.

Failures. llm.complete never raises; a call that got no answer is told apart
from an empty reply by its on_usage hook not firing. Such a stage is tried once
on fallback_tier inside the same cap, and the run result names it. If that also
fails the run stops and nothing is saved.

Caching. A stage whose calls share a long prefix in the user message names
the text that ends the shared part (`cache_until` in the config): the rewrite
and its retry share the posting, the playbook and the resume; the judge's two
orderings share the posting and the instructions. That prefix goes as its own
cached block (5-minute TTL), and the stage's first call runs alone so the
others read what it wrote: a cache entry is readable only once a response has
begun, and these calls do not stream, so the rest wait for it to finish.
The engine's system prompts are 40 to 70 tokens, below the 512-token minimum,
so caching them alone would do nothing. The worst case of a cached call prices
every input byte at the cache-write rate, so the cap still holds.

Pages. The engine's page budget comes from apex_domains constants (two pages of
500 words for AI roles, none otherwise). page_budget() sets them from
config/resume_engine.yaml for the length of a run, without editing the file:
one page of page_words, for every role.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from core import costs, db, llm
from core.config import REPO_ROOT, settings
from core.resume_engine import apex_domains
from core.resume_engine import apex_engine as E

log = logging.getLogger(__name__)

CONFIG_PATH = REPO_ROOT / "config" / "resume_engine.yaml"
PURPOSE_PREFIX = "resume_engine/"

# The engine's prompts, by stage. Atlas's own engine-side prompts (the posting's
# keywords) register here too, so every call is routed and booked by stage.
STAGE_PROMPTS: dict[str, str] = {
    E.DIAGNOSE_SYSTEM: "diagnose",
    E.REWRITE_SYSTEM: "rewrite",
    E.VERIFY_SYSTEM: "audit",
    E.REPAIR_SYSTEM: "repair",
    E.JUDGE_SYSTEM: "judge",
    E.CUT_SYSTEM: "cut",
}


def stage_of(system: str) -> str:
    return STAGE_PROMPTS.get(system, "other")


# ------------------------------------------------------------------ config


class Route(BaseModel):
    tier: str = "scan"
    provider: str = "anthropic"
    # Text that ends the prefix this stage's calls share (cached up to, not
    # including, its first occurrence); None: no caching for the stage.
    cache_until: str | None = None


class EngineConfig(BaseModel):
    stages: dict[str, Route] = Field(default_factory=dict)
    default: Route = Field(default_factory=Route)
    fallback_tier: str | None = "scan"
    page_words: int = 580
    thinking_headroom_tokens: int = 8000
    nightly: bool = False

    def route(self, stage: str) -> Route:
        return self.stages.get(stage, self.default)


def load_config(path: Path | None = None) -> EngineConfig:
    data = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    return EngineConfig.model_validate(data)


# ------------------------------------------------------------------ money


class BudgetExceeded(RuntimeError):
    """A call could not be made without risking a pass of the run's cap."""


class EngineCallFailed(RuntimeError):
    """A model call got no answer (network, quota, provider down)."""


def worst_case_usd(model: str, system: str, user: str, max_tokens: int, *, cached: bool = False) -> Decimal:
    """The most this call can cost: each input byte a token (at the cache-write
    rate when the call writes a cache entry), the whole output budget used."""
    input_bound = len(system.encode("utf-8")) + len(user.encode("utf-8")) + 64
    kind = "cache_creation_input_tokens" if cached else "input_tokens"
    return costs.cost_of_usage(model, {kind: input_bound, "output_tokens": max_tokens})


def cache_prefix(route: Route, user: str) -> str | None:
    """The shared prefix to cache for this call, or None."""
    if not route.cache_until:
        return None
    at = user.find(route.cache_until)
    return user[:at] if at > 0 else None


class RunBudget:
    """Reserve-then-settle accounting for one run, shared by the engine's threads."""

    def __init__(self, cap: Decimal, *, wait_seconds: float = 600.0) -> None:
        self.cap = cap
        self.spent = Decimal("0")
        self.reserved = Decimal("0")
        self.peak = Decimal("0")  # the highest spent + reserved ever held
        self.stopped: str | None = None
        self._wait = wait_seconds
        self._cond = threading.Condition()

    def reserve(self, amount: Decimal, stage: str) -> Decimal:
        with self._cond:
            while True:
                if self.stopped:
                    raise BudgetExceeded(self.stopped)
                if self.spent + amount > self.cap:
                    self.stopped = (
                        f"stopped before {stage}: it could cost up to ${amount:.4f} and "
                        f"${self.cap - self.spent:.4f} of the ${self.cap:.2f} cap is left"
                    )
                    self._cond.notify_all()
                    raise BudgetExceeded(self.stopped)
                if self.spent + self.reserved + amount <= self.cap:
                    self.reserved += amount
                    self.peak = max(self.peak, self.spent + self.reserved)
                    return amount
                # It fits once calls in flight settle. Wait for them.
                if not self._cond.wait(timeout=self._wait):
                    self.stopped = f"stopped before {stage}: waited {self._wait:.0f}s for calls in flight"
                    self._cond.notify_all()
                    raise BudgetExceeded(self.stopped)

    def settle(self, reserved: Decimal, actual: Decimal, stage: str) -> None:
        with self._cond:
            self.reserved -= reserved
            self.spent += actual
            if actual > reserved and not self.stopped:
                # The bound above should make this impossible; if it ever happens,
                # make no further calls rather than trust the estimate again.
                log.error("resume engine %s cost $%s over its $%s reservation", stage, actual, reserved)
                self.stopped = f"{stage} cost more than its reservation; no further calls"
            self._cond.notify_all()


@dataclass
class Call:
    stage: str
    model: str
    tier: str
    reserved_usd: Decimal
    cost_usd: Decimal
    answered: bool
    fallback: bool = False
    usage: dict[str, int] = field(default_factory=dict)


CompleteFn = Callable[..., Coroutine[Any, Any, str]]


class EngineLLM:
    """The engine's `llm` callable for one run. Thread-safe; call it from worker threads only."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        budget: RunBudget,
        config: EngineConfig,
        complete: CompleteFn | None = None,
    ) -> None:
        self.loop = loop
        self.budget = budget
        self.config = config
        self._complete: CompleteFn = complete or llm.complete
        self._lock = threading.Lock()
        self.calls: list[Call] = []
        self.fallbacks: list[dict[str, str]] = []
        self._warm: dict[str, threading.Event] = {}

    def _first_or_wait(self, key: str) -> threading.Event | None:
        """The event to set when this call is the first with this prefix, or None
        after waiting (up to 5 minutes, the cache's lifetime) for the first to finish."""
        with self._lock:
            event = self._warm.get(key)
            if event is None:
                self._warm[key] = event = threading.Event()
                return event
        event.wait(timeout=300)
        return None

    def __call__(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 3000,
        json_mode: bool = False,
        effort: str | None = None,
        **_: Any,
    ) -> str:
        stage = stage_of(system)
        route = self.config.route(stage)
        try:
            return self._call(stage, route.tier, route.provider, system, user, max_tokens, effort, False)
        except EngineCallFailed as exc:
            fb = self.config.fallback_tier
            if not fb:
                raise
            log.warning("resume engine %s failed on %s/%s, falling back to %s: %s",
                        stage, route.provider, route.tier, fb, exc)
            with self._lock:
                self.fallbacks.append({"stage": stage, "from": f"{route.provider}/{route.tier}",
                                       "to": f"anthropic/{fb}", "reason": str(exc)})
            return self._call(stage, fb, "anthropic", system, user, max_tokens, effort, True)

    def _call(
        self,
        stage: str,
        tier: str,
        provider: str,
        system: str,
        user: str,
        max_tokens: int,
        effort: str | None,
        fallback: bool,
    ) -> str:
        model = llm.resolve_model(tier, provider=provider)
        send = max_tokens + self.config.thinking_headroom_tokens
        prefix = None if provider != "anthropic" else cache_prefix(self.config.route(stage), user)
        first = self._first_or_wait(f"{stage}:{model}:{hash(prefix)}") if prefix else None
        try:
            reserved = self.budget.reserve(
                worst_case_usd(model, system, user, send, cached=bool(prefix)), stage)
        except BaseException:
            if first is not None:
                first.set()
            raise
        seen: dict[str, Any] = {}

        def hook(answered_by: str, usage: dict[str, int], cost: Decimal) -> None:
            seen.update(model=answered_by, cost=cost, usage=dict(usage))

        extra: dict[str, Any] = {"cache_prefix": prefix} if prefix else {}
        try:
            future: Future[str] = asyncio.run_coroutine_threadsafe(
                self._complete(system=system, user=user, tier=tier, provider=provider,
                               max_tokens=send, purpose=PURPOSE_PREFIX + stage, effort=effort,
                               on_usage=hook, **extra),
                self.loop,
            )
            text = future.result()
        finally:
            if first is not None:
                first.set()
            cost = Decimal(str(seen.get("cost", "0")))
            self.budget.settle(reserved, cost, stage)
            with self._lock:
                self.calls.append(Call(stage, str(seen.get("model", model)), tier, reserved, cost,
                                       "cost" in seen, fallback, seen.get("usage") or {}))
        if "cost" not in seen:
            raise EngineCallFailed(f"{stage}: no answer from {model}")
        return text


# ------------------------------------------------------------------ pages

_page_lock = threading.Lock()
_page_state: dict[str, Any] = {"depth": 0, "words": None, "saved": None}


@contextmanager
def page_budget(words: int) -> Iterator[None]:
    """One page of `words` for every role while the block runs; the engine's own
    constants come back afterwards. Runs that overlap must agree on `words`."""
    with _page_lock:
        if _page_state["depth"] and _page_state["words"] != words:
            raise RuntimeError("two resume runs asked for different page budgets at once")
        if not _page_state["depth"]:
            _page_state["saved"] = (apex_domains.PAGES, apex_domains.WORDS_PER_PAGE)
            pages: dict[str | None, int] = dict.fromkeys(apex_domains.PAGES, 1)
            pages[None] = 1  # page_budget(None) is consulted for roles outside the AI niche
            # The engine's dict is keyed by domain name; page_budget() also looks up None.
            apex_domains.PAGES = pages  # type: ignore[assignment]
            apex_domains.WORDS_PER_PAGE = words
            _page_state["words"] = words
        _page_state["depth"] += 1
    try:
        yield
    finally:
        with _page_lock:
            _page_state["depth"] -= 1
            if not _page_state["depth"]:
                apex_domains.PAGES, apex_domains.WORDS_PER_PAGE = _page_state["saved"]
                _page_state["words"] = None


# ------------------------------------------------------------------ a run


@dataclass
class Meter:
    """What one run spent and how."""

    cap_usd: Decimal
    spent_usd: Decimal = Decimal("0")
    peak_usd: Decimal = Decimal("0")
    calls: list[Call] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    stopped: str | None = None
    log: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        by_stage: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            s = by_stage.setdefault(c.stage, {"calls": 0, "cost_usd": Decimal("0"), "models": set(),
                                              "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            s["calls"] += 1
            s["cost_usd"] += c.cost_usd
            s["models"].add(c.model)
            s["input"] += c.usage.get("input_tokens", 0)
            s["output"] += c.usage.get("output_tokens", 0)
            s["cache_read"] += c.usage.get("cache_read_input_tokens", 0)
            s["cache_write"] += c.usage.get("cache_creation_input_tokens", 0)
        return {
            "cap_usd": str(self.cap_usd),
            "spent_usd": str(self.spent_usd),
            "stages": {k: {"calls": v["calls"], "cost_usd": str(v["cost_usd"]), "models": sorted(v["models"]),
                           "input": v["input"], "output": v["output"], "cache_read": v["cache_read"],
                           "cache_write": v["cache_write"]} for k, v in by_stage.items()},
            "fallbacks": self.fallbacks,
            "stopped": self.stopped,
        }


async def resume_spend_today() -> Decimal:
    """What resume-engine calls have cost so far today (UTC)."""
    val = await db.fetchval(
        "select coalesce(sum(cost_usd), 0) from llm_calls "
        "where purpose like $1 and called_at::date = $2",
        PURPOSE_PREFIX + "%",
        costs.spend_today(),
    )
    return Decimal(str(val or 0))


async def run_cap(
    cap_usd: Decimal | None = None,
    *,
    daily_cap_usd: Decimal | None = None,
) -> tuple[Decimal, str]:
    """The cap for one run and which limit set it. Raises costs.CostCapExceeded
    when Atlas's daily cap is already spent."""
    await costs.enforce_daily_cap()
    run = Decimal(str(cap_usd if cap_usd is not None else settings.resume_run_usd_cap))
    sub = Decimal(str(daily_cap_usd if daily_cap_usd is not None else settings.resume_daily_usd_cap))
    sub_left = sub - await resume_spend_today()
    day_left = Decimal(str(settings.daily_usd_cap)) - (await costs.daily_spend())["total"]
    limits = [
        (run, "the run cap"),
        (sub_left, "what is left of today's resume-engine cap"),
        (day_left, "what is left of today's Atlas cap"),
    ]
    cap, why = min(limits, key=lambda x: x[0])
    return max(cap, Decimal("0")), why


async def call_engine[T](
    fn: Callable[[EngineLLM], T],
    *,
    cap_usd: Decimal,
    config: EngineConfig | None = None,
    complete: CompleteFn | None = None,
) -> tuple[T | None, Meter]:
    """Run fn(engine_llm) in a worker thread under the page budget and a cap of
    cap_usd. Returns (fn's result, the meter), or (None, meter) when the run
    stopped at the cap or a call failed twice; meter.stopped says why."""
    config = config or load_config()
    meter = Meter(cap_usd=cap_usd)
    budget = RunBudget(cap_usd)
    engine_llm = EngineLLM(asyncio.get_running_loop(), budget, config, complete)
    out: T | None = None
    try:
        with page_budget(config.page_words):
            out = await asyncio.to_thread(fn, engine_llm)
    except BudgetExceeded as exc:
        meter.stopped = str(exc)
    except EngineCallFailed as exc:
        meter.stopped = f"a model call failed twice: {exc}"
    meter.spent_usd, meter.peak_usd = budget.spent, budget.peak
    meter.calls, meter.fallbacks = list(engine_llm.calls), list(engine_llm.fallbacks)
    return out, meter


__all__ = [
    "BudgetExceeded",
    "Call",
    "EngineCallFailed",
    "EngineConfig",
    "EngineLLM",
    "Meter",
    "Route",
    "RunBudget",
    "STAGE_PROMPTS",
    "cache_prefix",
    "call_engine",
    "load_config",
    "page_budget",
    "resume_spend_today",
    "run_cap",
    "stage_of",
    "worst_case_usd",
]
