"""The trust ladder (core/trust.py): every rung with its record, and his switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from core import db, trust
from core.runtime import policy
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"X-Atlas-Client": "1", "Origin": "http://atlas"}


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, "approval_policy", "actions", "approvals", "episodes"):
        policy.invalidate_cache()
        await policy.set_policy("*send_*", "approve", "Messages leave only after an approval card")
        await policy.set_policy("*payment*", "deny", "Money movement is never automated")
        await policy.set_policy("read_*", "auto", "Reads are free")
        yield
        policy.invalidate_cache()


async def _calls(tool: str, n: int, *, errors: int = 0, days_ago: float = 1) -> None:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    for i in range(n):
        out = {"error": "boom"} if i < errors else {"result": "ok"}
        await db.execute("insert into actions (agent_name, action_type, inputs, outputs, created_at) values ('career', 'tool_call', $1, $2, $3)",
                         {"tool": tool, "input": {}}, out, at + timedelta(seconds=i))


async def _asked(tool: str, status: str, *, days_ago: float = 1) -> None:
    await db.execute(
        "insert into approvals (agent_name, action_summary, action_payload, status, expires_at, created_at) "
        "values ('career', $1, $2, $3, now() + interval '1 day', $4)",
        f"career wants to run {tool}", {"tool": tool, "input": {}}, status, datetime.now(UTC) - timedelta(days=days_ago),
    )


@aio
async def test_the_ladder_shows_every_rung_with_its_record(world):
    await _calls("gmail_send_message", 3)
    await _calls("read_resume", 12, errors=1)
    await _calls("read_calendar", 2)
    await _calls("web_fetch", 5)                     # fenced by nothing: refused, no policy yet
    await _calls("gmail_send_message", 4, days_ago=45)  # outside the window
    await _asked("gmail_send_message", "approved")
    await _asked("gmail_send_message", "approved")
    await _asked("gmail_send_message", "rejected")
    await _asked("stripe_payment", "expired")

    out = await trust.ladder(30)
    rungs = {r["pattern"]: r for r in out["rungs"]}
    assert [r["pattern"] for r in out["rungs"]] == ["*payment*", "*send_*", "read_*", "web_fetch"]
    assert out["counts"] == {"deny": 1, "approve": 1, "auto": 1, "unmatched": 1}
    send = rungs["*send_*"]
    assert send["rung"] == "asks first" and send["tools"] == ["gmail_send_message"] and send["calls"] == 3
    assert (send["asked"], send["approved"], send["rejected"], send["expired"]) == (3, 2, 1, 0) and send["fenced"]
    reads = rungs["read_*"]
    assert reads["tools"] == ["read_calendar", "read_resume"] and reads["calls"] == 14 and reads["errors"] == 1
    assert reads["last_at"] is not None and reads["note"] == "Reads are free"
    pay = rungs["*payment*"]
    assert pay["rung"] == "never" and pay["tools"] == ["stripe_payment"] and pay["calls"] == 0 and pay["expired"] == 1
    free = rungs["web_fetch"]
    assert free["fenced"] is False and free["gate"] == "unmatched" and free["calls"] == 5 and free["note"] is None
    assert free["rung"] == "refused: no policy yet"

    # A wider window counts the old calls too.
    assert {r["pattern"]: r["calls"] for r in (await trust.ladder(60))["rungs"]}["*send_*"] == 7


@aio
async def test_his_switch_fences_a_tool_moves_a_rung_and_takes_the_fence_off(world):
    await _calls("web_fetch", 2)
    fenced = await trust.set_gate("web_fetch", "approve", "Fetches ask first for now")
    assert fenced["fenced"] and fenced["rung"] == "asks first" and fenced["tools"] == ["web_fetch"] and fenced["calls"] == 2
    assert (await policy.gate_for("web_fetch")) == ("approve", "web_fetch")
    moved = await trust.set_gate("*send_*", "deny")
    assert moved["rung"] == "never" and moved["note"] == "Messages leave only after an approval card"   # note kept
    assert await trust.remove("web_fetch") and not await trust.remove("web_fetch")
    assert [r["pattern"] for r in (await trust.ladder())["rungs"] if not r["fenced"]] == ["web_fetch"]
    with pytest.raises(ValueError):
        await trust.set_gate("x", "maybe")
    with pytest.raises(ValueError):
        await trust.set_gate("  ", "auto")


@aio
async def test_routes(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        r = await c.get("/api/trust")
        assert r.status_code == 200 and [x["pattern"] for x in r.json()["rungs"]] == ["*payment*", "*send_*", "read_*"]
        r = await c.post("/api/trust", json={"pattern": "web_fetch", "gate": "deny", "note": "No fetching tonight"})
        assert r.status_code == 200 and r.json()["rung"] == "never" and r.json()["fenced"]
        assert (await c.post("/api/trust", json={"pattern": "web_fetch", "gate": "sometimes"})).status_code == 422
        assert (await c.post("/api/trust/remove", json={"pattern": "web_fetch"})).json() == {"removed": "web_fetch"}
        assert (await c.post("/api/trust/remove", json={"pattern": "web_fetch"})).status_code == 404
        assert (await c.get("/api/trust", params={"days": 7})).json()["days"] == 7


@aio
async def test_money_and_account_rules_are_locked_on_never(world):
    ladder = {r["pattern"]: r for r in (await trust.ladder())["rungs"]}
    assert ladder["*payment*"]["locked"] and ladder["*payment*"]["label"] == "Payments"
    assert not ladder["*send_*"]["locked"] and ladder["*send_*"]["label"] == "Sending messages"
    with pytest.raises(trust.LockedRule):
        await trust.set_gate("*payment*", "auto")
    with pytest.raises(trust.LockedRule):
        await trust.remove("*payment*")
    assert (await policy.gate_for("pay_invoice_payment"))[0] == "deny"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        assert (await c.post("/api/trust", json={"pattern": "*payment*", "gate": "approve"})).status_code == 403
        assert (await c.post("/api/trust/remove", json={"pattern": "*payment*"})).status_code == 403
    assert (await trust.set_gate("*payment*", "deny"))["rung"] == "never", "keeping it on never is fine"


@aio
async def test_sending_and_submitting_can_ask_first_but_never_run_on_their_own(world):
    with pytest.raises(trust.LockedRule):
        await trust.set_gate("*send_*", "auto")
    assert (await trust.set_gate("*submit*", "auto"))["rung"] == "on its own", "submitting forms is his call"
    assert (await trust.set_gate("*send_*", "deny"))["rung"] == "never"
    assert (await trust.set_gate("*send_*", "approve"))["rung"] == "asks first"
    row = next(r for r in (await trust.ladder())["rungs"] if r["pattern"] == "*send_*")
    assert row["no_auto"] and not row["locked"]


def test_notes_drop_migration_bookkeeping():
    assert trust._note("Gmail drafts are approved first (privacy gate, 035)") == "Gmail drafts are approved first"
    assert trust._note("Reads are free") == "Reads are free"
    assert trust._note(None) is None
