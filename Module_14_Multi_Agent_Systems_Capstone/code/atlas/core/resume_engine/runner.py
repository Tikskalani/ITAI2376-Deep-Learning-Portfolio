"""One engine run, start to finish: format in, checked one-page draft out.

optimize() and revise() wrap the engine's entry points with everything Atlas
needs around them: the resume crosses resume_format's checked seam both ways,
the cap comes from bridge.run_cap(), the engine runs under the page budget, and
a draft that still renders past one page is refused with the reason. A refused
draft has text None and refused set; nothing should be saved from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core import costs
from core.jobs import resume_pdf
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, guards
from core.resume_engine import resume_format as RF

# Runs in the engine's thread with the run's model, before the engine starts.
Prepare = Callable[[bridge.EngineLLM, dict[str, Any]], dict[str, Any]]


@dataclass
class Draft:
    text: str | None
    result: dict[str, Any] | None
    meter: bridge.Meter | None
    refused: str | None = None
    target: dict[str, Any] | None = None  # the target the engine ran on (after prepare)


def page_refusal(text: str, page_words: int) -> str | None:
    """Why a draft cannot go out on one page, or None when it fits."""
    rendered = resume_pdf.render(text)
    if rendered.fits:
        return None
    words = len(text[text.find("# "):].split()) if "# " in text else len(text.split())
    return (f"The draft is {words} words and needs {rendered.pages} pages even at "
            f"{resume_pdf.SIZES[-1]}pt; one page holds about {page_words}. Approve a shorter "
            "one-page master (python -m scripts.resume_one_page propose) or answer fewer "
            "questions with long detail.")


async def _run(
    op: str,
    resume: str,
    target: dict[str, Any],
    answers: dict[str, str] | None,
    *,
    cap_usd: Decimal | None,
    daily_cap_usd: Decimal | None,
    config: bridge.EngineConfig | None,
    complete: bridge.CompleteFn | None,
    extra: dict[str, Any],
    prepare: Prepare | None = None,
) -> Draft:
    config = config or bridge.load_config()
    try:
        engine_text = RF.to_engine(resume)
    except RF.FormatMismatch as exc:
        return Draft(None, None, None, f"The resume cannot go through the engine safely: {exc}")
    try:
        cap, why = await bridge.run_cap(cap_usd, daily_cap_usd=daily_cap_usd)
    except costs.CostCapExceeded as exc:
        return Draft(None, None, None, str(exc))
    if cap <= Decimal("0.01"):
        return Draft(None, None, None, f"No budget for a run: {why} is ${cap:.2f}.")

    lines: list[str] = []
    fn = E.optimize if op == "optimize" else E.revise
    used: dict[str, Any] = {"target": target}

    def go(engine_llm: bridge.EngineLLM) -> dict[str, Any]:
        t = prepare(engine_llm, target) if prepare else target
        used["target"] = t
        result: dict[str, Any] = fn(engine_llm, engine_text, t, answers, log=lines.append, **extra)
        return result

    result, meter = await bridge.call_engine(go, cap_usd=cap, config=config, complete=complete)
    meter.log = lines
    t = used["target"]
    if result is None:
        return Draft(None, None, meter, f"The run stopped and nothing was saved: {meter.stopped}.", t)
    guarded = guards.summary_guard(engine_text, result)
    if guarded:
        lines.append(guarded)
    try:
        draft = RF.from_engine(result["final"], engine_text)
    except RF.FormatMismatch as exc:
        return Draft(None, result, meter, f"The engine's draft changed lines it must not: {exc}", t)
    refusal = page_refusal(draft, config.page_words)
    if refusal:
        return Draft(None, result, meter, refusal, t)
    return Draft(draft, result, meter, None, t)


async def optimize(
    resume: str,
    target: dict[str, Any],
    answers: dict[str, str] | None = None,
    *,
    cap_usd: Decimal | None = None,
    daily_cap_usd: Decimal | None = None,
    config: bridge.EngineConfig | None = None,
    complete: bridge.CompleteFn | None = None,
    prepare: Prepare | None = None,
) -> Draft:
    """A tailored draft of `resume` (resume_pdf format) for `target`.
    prepare(engine_llm, target) -> target runs first, inside the same cap
    (the posting's keywords are read there)."""
    return await _run("optimize", resume, target, answers, cap_usd=cap_usd,
                      daily_cap_usd=daily_cap_usd, config=config, complete=complete,
                      extra={"judge_it": True}, prepare=prepare)


async def revise(
    resume: str,
    target: dict[str, Any],
    answers: dict[str, str],
    *,
    kept: dict[str, list[str]] | None = None,
    prior_questions: list[dict[str, Any]] | None = None,
    order: list[Any] | None = None,
    section_order: list[str] | None = None,
    cap_usd: Decimal | None = None,
    daily_cap_usd: Decimal | None = None,
    config: bridge.EngineConfig | None = None,
    complete: bridge.CompleteFn | None = None,
) -> Draft:
    """The first run's draft rewritten only where answers came in."""
    return await _run("revise", resume, target, answers, cap_usd=cap_usd,
                      daily_cap_usd=daily_cap_usd, config=config, complete=complete,
                      extra={"kept": kept, "prior_questions": prior_questions,
                             "order": order or (), "section_order": section_order,
                             "judge_it": True})


__all__ = ["Draft", "optimize", "page_refusal", "revise"]
