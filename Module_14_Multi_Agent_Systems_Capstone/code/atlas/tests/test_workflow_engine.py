"""Tests for the durable workflow engine (core/workflow_engine.py) on the
live test database. Dispatch and notify are fakes; approvals are real
rows decided through the broker, which is the point."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from core import db
from core import workflow_engine as eng
from core.runtime import approval as ap
from core.surfaces import notify
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # The fake push still marks every subscription used; the phone's real one
    # stays untouched. Notifications stay real: approvals point at them.
    notify.set_senders(push=AsyncMock(return_value=201))
    async with livedb.shadow(monkeypatch, "push_subscriptions"):
        yield
        notify.set_senders()
        await db.execute("delete from workflow_runs where name like 'test_%'")
        await db.execute("delete from approvals where agent_name like 'workflow/test_%'")
        await db.execute("delete from actions where agent_name like 'workflow/test_%'")
        await db.execute(
            "delete from notifications where kind in ('workflow', 'approval') and (title like '%test_%'"
            " or data->>'workflow' like 'test_%' or data->>'agent' like 'workflow/test_%')"
        )


@pytest.fixture
def wf_dir(tmp_path: Path) -> Path:
    (tmp_path / "test_linear.yml").write_text(textwrap.dedent("""
        name: test_linear
        steps:
          - agent: cfo
            task: "Summarize {month} for {owner_input}"
          - agent: ceo
            task: "Brief from: {step_1_output}"
    """))
    (tmp_path / "test_approval.yml").write_text(textwrap.dedent("""
        name: test_approval
        steps:
          - agent: client_delivery
            task: "Draft for {owner_input}"
          - type: approval
            summary: "Send draft for {owner_input}?"
            payload:
              text: "{step_1_output}"
          - agent: client_delivery
            task: "Create draft: {step_1_output}"
          - type: notify
            title: "Draft ready for {owner_input}"
            body: "{step_3_output}"
    """))
    (tmp_path / "test_wait.yml").write_text(textwrap.dedent("""
        name: test_wait
        steps:
          - type: wait_until
            delay_minutes: 30
          - agent: coo
            task: "after the wait"
    """))
    (tmp_path / "test_condition.yml").write_text(textwrap.dedent("""
        name: test_condition
        steps:
          - agent: cfo
            task: "check"
          - type: condition
            source: "{step_1_output}"
            contains: "PROBLEM"
            on_false: skip_next
          - type: notify
            title: "Problem found"
            body: "{step_1_output}"
          - agent: ceo
            task: "wrap up"
    """))
    (tmp_path / "test_stop.yml").write_text(textwrap.dedent("""
        name: test_stop
        steps:
          - agent: cfo
            task: "check"
          - type: condition
            source: "{step_1_output}"
            equals: "go"
            on_false: stop
          - agent: ceo
            task: "never"
    """))
    return tmp_path


def _dispatch(replies: dict[str, str] | None = None, fail: set[str] | None = None):
    calls: list[tuple[str, str]] = []

    async def fake(agent, task, **kw):
        calls.append((agent, task))
        if fail and agent in fail:
            raise RuntimeError(f"{agent} down")
        return SimpleNamespace(text=(replies or {}).get(agent, f"{agent} says ok"))

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


async def test_load_definition_normalises_types_and_validates(wf_dir):
    d = eng.load_definition("test_approval", root=wf_dir)
    assert [s["type"] for s in d["steps"]] == ["agent", "approval", "agent", "notify"]
    (wf_dir / "test_bad.yml").write_text("name: test_bad\nsteps:\n  - type: approval\n")
    with pytest.raises(ValueError):
        eng.load_definition("test_bad", root=wf_dir)
    with pytest.raises(FileNotFoundError):
        eng.load_definition("nope", root=wf_dir)


async def test_linear_run_completes_chains_outputs_and_notifies_once(wf_dir):
    fake = _dispatch({"cfo": "net +$1,200", "ceo": "Good month."})
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        run = await eng.run_to_completion("test_linear", "Sidework", dispatch_fn=fake, root=wf_dir)
    assert run["status"] == "done" and run["final_output"] == "Good month."
    assert fake.calls[0][0] == "cfo" and "for Sidework" in fake.calls[0][1]
    assert fake.calls[1] == ("ceo", "Brief from: net +$1,200")
    steps = await eng.get_steps(run["id"])
    assert [s["status"] for s in steps] == ["done", "done"]
    assert steps[0]["output"] == {"text": "net +$1,200"} and steps[0]["attempts"] == 1
    note.assert_awaited_once()
    assert note.await_args.kwargs["title"] == "Workflow test_linear done"
    assert run["id"] in {r["id"] for r in await eng.list_runs(status="done")}


async def test_approval_step_waits_then_continues_after_the_card_is_approved(wf_dir):
    fake = _dispatch({"client_delivery": "Hi Chris, this week we shipped X."})
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)):
        run = await eng.run_to_completion("test_approval", "Demo Dental", dispatch_fn=fake, root=wf_dir)
    assert run["status"] == "waiting" and run["current_step"] == 2
    assert len(fake.calls) == 1
    steps = await eng.get_steps(run["id"])
    aid = steps[1]["waiting_on"]["approval_id"]
    card = await ap.check_approval(aid)
    assert card is not None and card["status"] == "pending"
    assert card["action_summary"] == "Send draft for Demo Dental?"
    assert card["action_payload"]["text"].startswith("Hi Chris") and card["action_payload"]["run_id"] == str(run["id"])
    assert card["notification_id"] is not None  # the card went out through the broker

    # The tick leaves it alone while the card is pending.
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)):
        out = await eng.tick(dispatch_fn=fake)
    assert out["still_waiting"] >= 1 and len(fake.calls) == 1

    assert await ap.decide_approval(aid, "approved") is not None
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        out = await eng.tick(dispatch_fn=fake)
    assert out["finished"] >= 1
    run = await eng.get_run(run["id"])
    assert run is not None and run["status"] == "done"
    assert fake.calls[1][1].startswith("Create draft: Hi Chris")
    steps = await eng.get_steps(run["id"])
    assert [s["status"] for s in steps] == ["done", "done", "done", "done"]
    # The recipe ended with its own notify step, so no extra outcome notification.
    titles = [c.kwargs["title"] for c in note.await_args_list]
    assert titles == ["Draft ready for Demo Dental"]


async def test_rejected_approval_cancels_the_run(wf_dir):
    fake = _dispatch()
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)):
        run = await eng.run_to_completion("test_approval", "X", dispatch_fn=fake, root=wf_dir)
    aid = (await eng.get_steps(run["id"]))[1]["waiting_on"]["approval_id"]
    await ap.decide_approval(aid, "rejected")
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        await eng.tick(dispatch_fn=fake)
    run = await eng.get_run(run["id"])
    assert run is not None and run["status"] == "cancelled" and "rejected" in run["error"]
    assert len(fake.calls) == 1
    assert note.await_args.kwargs["title"] == "Workflow test_approval cancelled"


async def test_wait_until_holds_then_releases_by_clock(wf_dir):
    fake = _dispatch()
    t0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    run_id = await eng.start_run("test_wait", root=wf_dir)
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)):
        run = await eng.advance_run(run_id, dispatch_fn=fake, now=t0)
        assert run["status"] == "waiting" and fake.calls == []
        out = await eng.tick(dispatch_fn=fake, now=t0 + timedelta(minutes=10))
        assert out["still_waiting"] >= 1 and fake.calls == []
        out = await eng.tick(dispatch_fn=fake, now=t0 + timedelta(minutes=31))
    run = await eng.get_run(run_id)
    assert run is not None and run["status"] == "done" and fake.calls == [("coo", "after the wait")]


async def test_condition_skip_next_and_stop(wf_dir):
    fake = _dispatch({"cfo": "all fine"})
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        run = await eng.run_to_completion("test_condition", dispatch_fn=fake, root=wf_dir)
    assert run["status"] == "done"
    steps = await eng.get_steps(run["id"])
    assert [s["status"] for s in steps] == ["done", "done", "skipped", "done"]
    assert [c.kwargs["title"] for c in note.await_args_list] == ["Workflow test_condition done"]

    fake = _dispatch({"cfo": "PROBLEM: overdraft"})
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        run = await eng.run_to_completion("test_condition", dispatch_fn=fake, root=wf_dir)
    steps = await eng.get_steps(run["id"])
    assert [s["status"] for s in steps] == ["done", "done", "done", "done"]
    assert note.await_args_list[0].kwargs["title"] == "Problem found"

    fake = _dispatch({"cfo": "hold"})
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)):
        run = await eng.run_to_completion("test_stop", dispatch_fn=fake, root=wf_dir)
    assert run["status"] == "done" and run["final_output"] == "hold"
    assert fake.calls == [("cfo", "check")]


async def test_agent_failure_retries_on_later_ticks_then_fails(wf_dir):
    fake = _dispatch(fail={"cfo"})
    t0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    run_id = await eng.start_run("test_linear", root=wf_dir)
    with patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)) as note:
        run = await eng.advance_run(run_id, dispatch_fn=fake, now=t0)
        assert run["status"] == "waiting"
        step = (await eng.get_steps(run_id))[0]
        assert step["attempts"] == 1 and "cfo down" in step["error"] and step["waiting_on"]["retry_after"]
        # Too early: the tick does not retry yet.
        await eng.tick(dispatch_fn=fake, now=t0 + timedelta(minutes=1))
        assert len(fake.calls) == 1
        await eng.tick(dispatch_fn=fake, now=t0 + timedelta(minutes=6))
        await eng.tick(dispatch_fn=fake, now=t0 + timedelta(minutes=30))
    run = await eng.get_run(run_id)
    assert run is not None and run["status"] == "failed" and "step 1 (cfo)" in run["error"]
    assert len(fake.calls) == 3
    assert note.await_args.kwargs["priority"] == "high"


async def test_cancel_run_and_unknown_run(wf_dir):
    run_id = await eng.start_run("test_wait", root=wf_dir)
    assert await eng.cancel_run(run_id) is True
    assert await eng.cancel_run(run_id) is False
    run = await eng.advance_run(run_id, dispatch_fn=_dispatch())
    assert run["status"] == "cancelled"

    with pytest.raises(ValueError):
        await eng.advance_run(uuid4(), dispatch_fn=_dispatch())


async def test_set_helpers_refuse_columns_outside_the_allowlist():
    with pytest.raises(ValueError, match="id"):
        await eng._set_run(uuid4(), id=uuid4())
    with pytest.raises(ValueError, match="name"):
        await eng._set_step(uuid4(), status="done", name="x; drop table workflow_steps")
    with pytest.raises(ValueError):
        await eng._set_run(uuid4())
