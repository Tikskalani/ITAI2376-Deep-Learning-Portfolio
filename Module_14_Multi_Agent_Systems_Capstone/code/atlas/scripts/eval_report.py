"""Run the offline evals, print them, and write docs/EVALS.md.

  python -m scripts.eval_report           print and rewrite docs/EVALS.md
  python -m scripts.eval_report --check   print only; exit 1 below a gate

No API calls. The live evals that spend tokens (python -m scripts.run_evals
<suite>) are separate and run deliberately.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from core.config import REPO_ROOT
from evals.offline import GATES, Report, run_all

OUT = REPO_ROOT / "docs" / "EVALS.md"

ABOUT = {
    "job_mail": "Gmail job classifier on the development set (the shapes it was built against).",
    "job_mail_holdout": "The same classifier on cases written after it, scored once and reported as they came out.",
    "job_filter": "Discovery filter (title, location, salary floor) on labeled postings.",
    "resume_engine": ("Resume engine on APEX's nine synthetic cases with a scripted adversarial model "
                      "(invented numbers, unsupported keywords, added skills, faithful edits, a summary "
                      "from a confirmed fact), through the real cap and page budget. Measures the gate "
                      "and the plumbing, not writing quality."),
}


def _gate_failures(r: Report) -> list[str]:
    return [f"{r.name}.{k} = {r.metrics.get(k)} < {v}" for k, v in GATES.get(r.name, {}).items()
            if r.metrics.get(k, 0) < v]


def render(reports: list[Report]) -> str:
    lines = [
        "# Atlas evals",
        "",
        f"Generated {date.today().isoformat()} by `python -m scripts.eval_report`. Offline: no API calls, "
        "so CI runs them on every change and fails the build below a gate (`evals/offline.py`, `GATES`).",
        "",
        "| Suite | Cases | Metric | Score | Gate |",
        "|---|---:|---|---:|---:|",
    ]
    for r in reports:
        for i, (k, v) in enumerate(r.metrics.items()):
            gate = GATES.get(r.name, {}).get(k)
            lines.append(f"| {r.name if i == 0 else ''} | {r.n if i == 0 else ''} | {k} | {v:.3f} | "
                         f"{'' if gate is None else f'{gate:.2f}'} |")
    lines += ["", "## What each suite measures", ""]
    lines += [f"- **{r.name}**: {ABOUT.get(r.name, '')}" for r in reports]
    misses = [(r.name, m) for r in reports for m in r.misses]
    lines += ["", "## Current misses", ""]
    lines += [f"- {n}: {m}" for n, m in misses] or ["None."]
    lines += [
        "",
        "## Reading these honestly",
        "",
        "- The development set was written from the shapes of real applicant-tracking mail and job boards, and the "
        "classifier was built against it, so its scores are an upper bound.",
        "- The holdout set was written afterwards and scored once. Its role-extraction score is the real number for "
        "unfamiliar phrasings; when a holdout miss is fixed, that case moves to the development set and new holdout "
        "cases are written.",
        "- The next holdout comes from real, redacted mail once Gmail is connected.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    reports = run_all()
    failures: list[str] = []
    for r in reports:
        print(f"{r.name} ({r.n} cases): " + ", ".join(f"{k} {v:.3f}" for k, v in r.metrics.items()))
        for m in r.misses:
            print(f"    miss: {m}")
        failures += _gate_failures(r)
    if not args.check:
        Path(OUT).write_text(render(reports), encoding="utf-8")
        print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    for f in failures:
        print(f"GATE FAILED: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
