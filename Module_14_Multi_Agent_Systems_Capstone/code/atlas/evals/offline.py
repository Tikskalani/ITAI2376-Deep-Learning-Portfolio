"""Offline evals: the deterministic parts of Atlas scored against labeled data.

No API calls, no cost, so they run in CI on every change:

  job_mail      the Gmail job classifier (core/jobs/mail.classify) on
                evals/data/job_mail.jsonl: is it job mail, which status,
                which company, which role
  job_filter    the discovery filter (core/jobs/discovery.keep) on
                evals/data/job_postings.jsonl: keep or drop
  resume_engine the resume engine's gate and plumbing on APEX's nine
                synthetic cases with a scripted adversarial model
                (evals/resume_bench): nothing invented ships, every
                attack is blocked, faithful edits survive, the cap holds

Each returns a Report with the metrics and every miss, so a regression
names the case that broke. scripts/eval_report.py prints them and writes
docs/EVALS.md; tests/test_eval_gates.py fails the build below the gates.

The data is a development set written by hand from the shapes of real
applicant-tracking mail and job boards. It is not held out: the classifier
was built against these shapes. A held-out set drawn from real (redacted)
mail is the next step once Gmail is connected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"


@dataclass
class Report:
    name: str
    n: int
    metrics: dict[str, float]
    misses: list[str] = field(default_factory=list)


def _load(name: str) -> list[dict[str, Any]]:
    with (DATA / name).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _ratio(a: int, b: int) -> float:
    return round(a / b, 3) if b else 1.0


def job_mail(dataset: str = "job_mail.jsonl", name: str = "job_mail") -> Report:
    from core.jobs.mail import classify

    cases = _load(dataset)
    tp = fp = fn = tn = 0
    status_ok = company_ok = role_ok = role_n = 0
    misses: list[str] = []
    for c in cases:
        v = classify(c["from"], c["subject"], c.get("snippet", ""))
        if c["relevant"] and v.relevant:
            tp += 1
            if v.status == c["status"]:
                status_ok += 1
            else:
                misses.append(f"{c['id']}: status {v.status!r}, expected {c['status']!r}")
            if (v.company or "").lower() == (c["company"] or "").lower():
                company_ok += 1
            else:
                misses.append(f"{c['id']}: company {v.company!r}, expected {c['company']!r}")
            if c.get("role"):
                role_n += 1
                if (v.role or "").lower() == c["role"].lower():
                    role_ok += 1
                else:
                    misses.append(f"{c['id']}: role {v.role!r}, expected {c['role']!r}")
        elif c["relevant"]:
            fn += 1
            misses.append(f"{c['id']}: missed job mail")
        elif v.relevant:
            fp += 1
            misses.append(f"{c['id']}: false alarm as {v.status!r}")
        else:
            tn += 1
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0
    return Report(
        name,
        len(cases),
        {
            "relevance_precision": precision,
            "relevance_recall": recall,
            "relevance_f1": f1,
            "status_accuracy": _ratio(status_ok, tp),
            "company_accuracy": _ratio(company_ok, tp),
            "role_accuracy": _ratio(role_ok, role_n),
        },
        misses,
    )


PROFILE_FOR_EVAL = {
    "titles": ["forward deployed", "ai solutions", "solutions engineer", "applied ai", "ai engineer",
               "llm engineer", "ai agent", "agentic", "ml engineer", "machine learning engineer",
               "automation engineer", "ai developer"],
    "exclude_titles": ["director", "vice president", "head of", "manager", "intern", "sales",
                       "account executive", "recruiter", "phd"],
    "locations": ["houston", "texas", "remote", "united states", "us"],
    "salary_floor_usd": 110000,
}


def job_filter(profile: dict[str, Any] | None = None) -> Report:
    from core.jobs.discovery import keep

    prof = profile or PROFILE_FOR_EVAL
    cases = _load("job_postings.jsonl")
    tp = fp = fn = tn = 0
    misses: list[str] = []
    for c in cases:
        got = keep({"title": c["title"], "location": c["location"], "remote": c["remote"],
                    "salary_max": c["salary_max"]}, prof)
        if got and c["keep"]:
            tp += 1
        elif got:
            fp += 1
            misses.append(f"{c['id']}: kept {c['title']!r} in {c['location']!r}")
        elif c["keep"]:
            fn += 1
            misses.append(f"{c['id']}: dropped {c['title']!r} in {c['location']!r}")
        else:
            tn += 1
    return Report(
        "job_filter",
        len(cases),
        {"accuracy": _ratio(tp + tn, len(cases)), "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn)},
        misses,
    )


def job_mail_holdout() -> Report:
    """Cases written after the classifier, scored once and reported as they
    came out; fixes made because of them move a case to the dev set."""
    return job_mail("job_mail_holdout.jsonl", "job_mail_holdout")


def resume_engine() -> Report:
    from evals import resume_bench

    report: Report = resume_bench.run()
    return report


SUITES = {"job_mail": job_mail, "job_mail_holdout": job_mail_holdout, "job_filter": job_filter,
          "resume_engine": resume_engine}

# The build fails below these (tests/test_eval_gates.py).
GATES = {
    "job_mail": {"relevance_precision": 0.95, "relevance_recall": 0.95, "status_accuracy": 0.9,
                 "company_accuracy": 0.9, "role_accuracy": 0.8},
    "job_filter": {"accuracy": 0.9, "precision": 0.9, "recall": 0.9},
    # Held out: only the relevance gate is enforced; the rest is reported.
    "job_mail_holdout": {"relevance_precision": 0.9, "relevance_recall": 0.9},
    "resume_engine": {"no_invented_rate": 1.0, "attack_blocked_rate": 1.0, "faithful_kept_rate": 0.95,
                      "evidence_used_rate": 0.9, "summary_kept_rate": 1.0, "protected_intact_rate": 1.0,
                      "questions_ok_rate": 1.0,
                      "under_cap_rate": 1.0},
}


def run_all() -> list[Report]:
    return [fn() for fn in SUITES.values()]


__all__ = ["GATES", "PROFILE_FOR_EVAL", "Report", "SUITES", "job_filter", "job_mail", "run_all"]
