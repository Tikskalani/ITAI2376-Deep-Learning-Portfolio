"""The resume bench measures something: with the engine's fact gate switched off,
the attacks reach the output and the gates fail. No model is called (the bench's
replies are scripted and its runs go through the bridge with a fake complete)."""

from __future__ import annotations

from core.resume_engine import apex_engine as E
from evals import resume_bench
from evals.offline import GATES


def test_the_bench_has_attacks_and_faithful_edits_in_every_case():
    for case in resume_bench.load_cases():
        target = resume_bench.make_target(case)
        kinds = [k for k, _ in resume_bench.Script(case, target).plan.values()]
        assert "invented" in kinds and "faithful" in kinds, case["name"]


def test_a_broken_gate_fails_the_bench(monkeypatch):
    monkeypatch.setattr(E, "fact_gate", lambda *a, **k: [])
    report = resume_bench.run()
    assert report.metrics["attack_blocked_rate"] < GATES["resume_engine"]["attack_blocked_rate"]
    assert any("reached the output" in m for m in report.misses)
