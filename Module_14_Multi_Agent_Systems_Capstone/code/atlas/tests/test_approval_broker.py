"""Tests for the approval broker (core/runtime/approval.py) on the live test
database, with notify's senders replaced so no push or email leaves."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio

from core import db
from core.runtime import approval as ap
from core.surfaces import notify
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")

AGENT = "test_broker_agent"


class _Push:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def __call__(self, sub, payload, urgency) -> int:
        self.payloads.append(payload)
        return 201


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    monkeypatch.setattr(notify.settings, "vapid_public_key", "pub")
    monkeypatch.setattr(notify.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(notify.settings, "vapid_subject", "mailto:t@example.com")
    monkeypatch.setattr(notify.settings, "smtp_host", "")
    # Cards push to every subscription; the phone's real one stays untouched.
    async with livedb.shadow(monkeypatch, "push_subscriptions"):
        await notify.add_subscription("https://push.example/broker", "k", "a")
        yield
        notify.set_senders()
        await db.execute("delete from actions where agent_name = $1", AGENT)
        await db.execute(
            "delete from notifications where id in (select notification_id from approvals where agent_name = $1)",
            AGENT,
        )
        await db.execute("delete from approvals where agent_name = $1", AGENT)
    ap._pending.clear()


async def _pending_id() -> object:
    row = await db.fetchrow(
        "select id from approvals where agent_name = $1 and status = 'pending' "
        "order by created_at desc limit 1", AGENT,
    )
    assert row is not None
    return row["id"]


async def test_request_then_approve_resolves_waiter_with_payload():
    push = _Push()
    notify.set_senders(push=push)
    task = asyncio.create_task(ap.request_approval(
        summary="send the invoice", payload={"to": "a@b.com"}, agent_name=AGENT,
    ))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if push.payloads:
            break
    card = push.payloads[0]
    assert card["kind"] == "approval" and card["priority"] == "high"
    assert card["title"] == "Approve: send the invoice"
    aid = card["data"]["approval_id"]
    assert "a@b.com" in card["body"]

    row = await ap.decide_approval(aid, "approved")
    assert row is not None and row["status"] == "approved"
    outcome = await task
    assert outcome.approved and outcome.payload == {"to": "a@b.com"}
    assert str(outcome.approval_id) == aid

    audit = await db.fetchrow(
        "select approved_by, outputs from actions where agent_name = $1 "
        "and action_type = 'approval_request' order by created_at desc limit 1", AGENT,
    )
    assert audit is not None and audit["approved_by"] == "user"
    assert audit["outputs"] == {"status": "approved"}
    # Read the link after the waiter returns: the decision can land before
    # the card is linked, so decide_approval's row may not carry it yet.
    linked = await ap.check_approval(aid)
    assert linked is not None and linked["notification_id"] is not None
    note = await db.fetchrow("select read_at from notifications where id = $1", linked["notification_id"])
    assert note is not None and note["read_at"] is not None  # the card is marked handled
    # Deciding twice is a no-op.
    assert await ap.decide_approval(aid, "rejected") is None


class _DecidingPush(_Push):
    """Decides the approval while the card is still going out: after the
    notification row exists but before the approval is linked to it."""

    async def __call__(self, sub, payload, urgency) -> int:
        await ap.decide_approval(payload["data"]["approval_id"], "approved")
        return await super().__call__(sub, payload, urgency)


async def test_decision_before_card_is_linked_still_marks_card_read():
    notify.set_senders(push=_DecidingPush())
    outcome = await ap.request_approval(summary="pay the bill", payload={"n": 1}, agent_name=AGENT)
    assert outcome.approved
    row = await ap.check_approval(outcome.approval_id)
    assert row is not None and row["notification_id"] is not None
    note = await db.fetchrow("select read_at from notifications where id = $1", row["notification_id"])
    assert note is not None and note["read_at"] is not None


async def test_reject_and_unknown_and_bad_decision():
    notify.set_senders(push=_Push())
    task = asyncio.create_task(ap.request_approval(summary="post it", payload={}, agent_name=AGENT))
    await asyncio.sleep(0.05)
    aid = await _pending_id()
    assert (await ap.decide_approval(aid, "rejected")) is not None
    outcome = await task
    assert outcome.status == "rejected" and outcome.payload is None and not outcome.approved
    assert await ap.decide_approval(uuid4(), "approved") is None
    with pytest.raises(ValueError):
        await ap.decide_approval(aid, "maybe")


async def test_short_wait_returns_pending_and_leaves_card_open():
    notify.set_senders(push=_Push())
    outcome = await ap.request_approval(
        summary="wire money", payload={"amount": 5}, agent_name=AGENT, wait_seconds=0.05,
    )
    assert outcome.status == "pending" and outcome.payload is None
    row = await ap.check_approval(outcome.approval_id)
    assert row is not None and row["status"] == "pending"
    assert outcome.approval_id in {r["id"] for r in await ap.list_pending_approvals()}
    # A late decision is still recorded, with nobody waiting.
    late = await ap.decide_approval(outcome.approval_id, "approved")
    assert late is not None and late["status"] == "approved"


async def test_full_ttl_wait_expires_the_row(monkeypatch):
    notify.set_senders(push=_Push())
    monkeypatch.setattr(ap.settings, "approval_ttl_hours", 0)  # TTL 0 -> wait 0 -> expire
    outcome = await ap.request_approval(summary="x", payload={}, agent_name=AGENT)
    assert outcome.status == "expired"
    row = await ap.check_approval(outcome.approval_id)
    assert row is not None and row["status"] == "expired"


async def test_expire_overdue_wakes_waiters():
    notify.set_senders(push=_Push())
    task = asyncio.create_task(ap.request_approval(summary="old", payload={}, agent_name=AGENT))
    await asyncio.sleep(0.05)
    aid = await _pending_id()
    await db.execute("update approvals set expires_at = now() - interval '1 minute' where id = $1", aid)
    assert await ap.expire_overdue_approvals() >= 1
    outcome = await task
    assert outcome.status == "expired"
    assert await ap.expire_overdue_approvals() == 0 or aid not in {r["id"] for r in await ap.list_pending_approvals()}


async def test_execute_on_approval_paths():
    calls: list[dict] = []

    async def executor(payload):
        calls.append(payload)
        return "sent"

    ok = ap.ApprovalOutcome(status="approved", payload={"to": "x"})
    assert await ap.execute_on_approval(ok, executor, agent_name=AGENT) == "sent"
    assert calls == [{"to": "x"}]
    no = ap.ApprovalOutcome(status="rejected", payload=None)
    assert await ap.execute_on_approval(no, executor, agent_name=AGENT) is None
    assert calls == [{"to": "x"}]

    async def boom(payload):
        raise RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        await ap.execute_on_approval(ok, boom, agent_name=AGENT)
    row = await db.fetchrow(
        "select outputs from actions where agent_name = $1 and action_type = 'action_executed' "
        "order by created_at desc limit 1", AGENT,
    )
    assert row is not None and "smtp down" in row["outputs"]["error"]
