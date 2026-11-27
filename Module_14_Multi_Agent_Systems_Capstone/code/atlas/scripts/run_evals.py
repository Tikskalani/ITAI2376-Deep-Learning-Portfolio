"""Run a named eval suite from the evals package.

Usage:
    python -m scripts.run_evals academic

Spends real Claude API tokens. Use deliberately. Results are persisted to
eval_runs so the Sunday review can compare pass rates before and after a
skill was adopted.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys

from core.runtime.evals import EvalCase, EvalResult, run_suite


async def _run(suite_name: str) -> int:
    try:
        module = importlib.import_module(f"evals.{suite_name}")
    except ModuleNotFoundError:
        print(f"no eval suite named {suite_name!r}")
        return 2
    cases: list[EvalCase] | None = getattr(module, "CASES", None)
    if not cases:
        print(f"suite {suite_name!r} has no CASES")
        return 2

    print(f"Running {len(cases)} case(s) in suite {suite_name!r}...")
    results: list[EvalResult] = await run_suite(cases)
    try:
        from core.skills import record_eval_results

        await record_eval_results(suite_name, results)
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not persist results: {exc})")
    for result in results:
        flag = "PASS" if result.passed else "FAIL"
        print(f"  {flag}  {result.case}")
        for detail in result.details:
            print(f"        {detail}")
    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


ALL_SUITES = (
    "academic", "build", "ceo", "cfo", "client_delivery", "coo", "cto",
    "news_discovery", "opportunity_scout", "personal_ops",
    "property_ops",
)


async def _run_all() -> int:
    total = 0
    passed = 0
    for suite in ALL_SUITES:
        print(f"\n=== suite: {suite} ===")
        rc = await _run(suite)
        if rc == 0:
            passed += 1
        total += 1
    print(f"\n{passed}/{total} suites passed.")
    return 0 if passed == total else 1


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: python -m scripts.run_evals <suite_name|all>\n"
            f"suites: all, {', '.join(ALL_SUITES)}"
        )
        sys.exit(2)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    suite = sys.argv[1]
    if suite == "all":
        sys.exit(asyncio.run(_run_all()))
    sys.exit(asyncio.run(_run(suite)))


if __name__ == "__main__":
    main()
