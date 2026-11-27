"""The ported APEX engine stays byte-identical to the tagged copy (core/resume_engine/PROVENANCE.md)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PINNED = {
    "core/resume_engine/apex_engine.py": "633bf99bc98f98cdcde356042937a15a00f4e6d8110f39b3dcbf10f3ccb888c3",
    "core/resume_engine/apex_domains.py": "c934cb444e89871629bcccc42f388795604803e241631565647185830b96aadf",
    "evals/resume_bench/apex_benchmark.py": "fc759340ddb701e047d655dc199dd15a2199e4435e84cbffc4cbb26673c7eb4b",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_engine_files_are_unmodified(name):
    digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    assert digest == PINNED[name], f"{name} drifted from apex-v11-final; see PROVENANCE.md"


def test_the_engine_finds_its_domains_module():
    from core.resume_engine import apex_domains, apex_engine

    assert apex_engine.apex_domains is apex_domains
