"""The offline evals are part of the build: below a gate, the suite fails."""

from __future__ import annotations

import pytest

from evals.offline import GATES, SUITES


@pytest.mark.parametrize("name", sorted(SUITES))
def test_suite_meets_its_gates(name):
    report = SUITES[name]()
    assert report.n > 0
    below = {k: report.metrics.get(k) for k, v in GATES.get(name, {}).items() if report.metrics.get(k, 0) < v}
    assert not below, f"{name} below gate: {below}; misses: {report.misses}"


def test_report_renders():
    from evals.offline import run_all
    from scripts.eval_report import render

    text = render(run_all())
    assert "| job_mail |" in text and "Reading these honestly" in text
