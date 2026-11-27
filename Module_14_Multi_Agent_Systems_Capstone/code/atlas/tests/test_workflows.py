"""Minimal tests for core/workflows.py -- happy path and step failure."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import workflows


def _write_workflow(tmp: Path, name: str, body: str) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"{name}.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_workflow_parses_valid_yaml():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _write_workflow(
            tmp,
            "demo",
            "name: demo\n"
            "description: a test\n"
            "steps:\n"
            "  - agent: cfo\n"
            "    task: pull P&L for {month}\n"
            "  - agent: ceo\n"
            "    task: synthesize {step_1_output}\n",
        )
        wf = workflows.load_workflow("demo", root=tmp)
    assert wf.name == "demo"
    assert len(wf.steps) == 2
    assert wf.steps[0].agent == "cfo"
    assert "{month}" in wf.steps[0].task


def test_load_workflow_missing_steps_raises():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _write_workflow(tmp, "bad", "name: bad\nsteps: []\n")
        with pytest.raises(ValueError):
            workflows.load_workflow("bad", root=tmp)


async def test_run_workflow_happy_path_chains_outputs():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _write_workflow(
            tmp,
            "chain",
            "name: chain\n"
            "description: chained\n"
            "steps:\n"
            "  - agent: cfo\n"
            "    task: 'pull stats for {month}'\n"
            "  - agent: ceo\n"
            "    task: 'synthesize: {step_1_output}'\n",
        )

        captured: list[str] = []

        async def fake_dispatch(agent, task, **kwargs):
            captured.append(task)
            return SimpleNamespace(text=f"output from {agent}")

        run = await workflows.run_workflow(
            "chain", root=tmp, dispatch_fn=fake_dispatch
        )
    assert run.ok is True
    assert run.completed == 2
    # The second step received the first step's output via substitution.
    assert "output from cfo" in captured[1]
    # {month} was resolved into the first task.
    assert "{month}" not in captured[0]


async def test_run_workflow_halts_on_step_failure():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _write_workflow(
            tmp,
            "boom",
            "name: boom\n"
            "description: x\n"
            "steps:\n"
            "  - agent: cfo\n"
            "    task: ok\n"
            "  - agent: coo\n"
            "    task: will crash\n"
            "  - agent: ceo\n"
            "    task: never reached\n",
        )

        async def fake_dispatch(agent, task, **kwargs):
            if agent == "coo":
                raise RuntimeError("simulated crash")
            return SimpleNamespace(text="ok")

        run = await workflows.run_workflow(
            "boom", root=tmp, dispatch_fn=fake_dispatch
        )
    assert run.ok is False
    # 1 step succeeded; the failing step's record is also present.
    assert run.completed == 1
    assert run.total == 3
    assert len(run.steps) == 2
    assert run.steps[1].ok is False
    assert "simulated crash" in run.steps[1].error
