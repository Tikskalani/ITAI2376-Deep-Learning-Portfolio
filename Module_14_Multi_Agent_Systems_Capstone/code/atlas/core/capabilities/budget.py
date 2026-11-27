"""Spend for one source: a worst-case reservation before every paid call.

The pilot cap (ATLAS_CAP_SOURCE_USD, $0.50) is a target, not a proven cost.
Before each call the stage's worst case is priced (input at the input rate,
max_tokens at the output rate, images by Anthropic's image-token rule) and
checked against what the source has left and what the day has left under
ATLAS_DAILY_USD_CAP. A stage that does not fit raises HeldForBudget: the
source is held (status held_budget), never run with a stage silently left
out. The real cost of each answer is booked as it arrives.

Limit of the guarantee: the provider bills after a call completes, so the
ceiling holds for each call's worst case as priced here; a price change at
the provider, or a call that runs without its max_tokens, is outside it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.config import settings
from core.costs import MODEL_PRICES, daily_spend, model_family
from core.llm import resolve_model

_M = Decimal(1_000_000)


class HeldForBudget(Exception):  # noqa: N818 - a state, not an error
    """The next stage would not fit the source's or the day's allowance."""

    def __init__(self, stage: str, need: Decimal, left: Decimal, which: str) -> None:
        super().__init__(f"{stage} needs up to ${need:.3f}; {which} has ${left:.3f} left")
        self.stage, self.need, self.left, self.which = stage, need, left, which


def image_tokens(width: int, height: int) -> int:
    """Anthropic's rule of thumb for an image's input tokens: (w * h) / 750."""
    return int(width * height / 750) + 1


def worst_case(model: str, text_chars: int, max_tokens: int, image_token_count: int = 0) -> Decimal:
    """The most one call can cost: text at ~3 characters a token, rounded up
    a third for tokenizer drift, plus images, at the input rate; max_tokens
    at the output rate."""
    prices = MODEL_PRICES[model_family(model)]
    tokens_in = Decimal(text_chars) / Decimal(3) * Decimal("1.33") + image_token_count
    return (tokens_in * prices["input"] + Decimal(max_tokens) * prices["output"]) / _M


class SourceBudget:
    """Tracks one processing attempt's spend against its source's cap."""

    def __init__(self, cap_usd: float, spent_usd: float = 0.0) -> None:
        self.cap = Decimal(str(cap_usd))
        self.spent = Decimal(str(spent_usd))

    def model(self) -> str:
        return resolve_model(settings.cap_tier, provider="anthropic")

    async def reserve(self, stage: str, text_chars: int, max_tokens: int, image_token_count: int = 0) -> Decimal:
        need = worst_case(self.model(), text_chars, max_tokens, image_token_count)
        left = self.cap - self.spent
        if need > left:
            raise HeldForBudget(stage, need, left, "this source")
        day_left = Decimal(str(settings.daily_usd_cap)) - (await daily_spend())["total"]
        if need > day_left:
            raise HeldForBudget(stage, need, day_left, "today's budget")
        return need

    def book(self, cost_usd: Any) -> None:
        self.spent += Decimal(str(cost_usd or 0))


__all__ = ["HeldForBudget", "SourceBudget", "image_tokens", "worst_case"]
