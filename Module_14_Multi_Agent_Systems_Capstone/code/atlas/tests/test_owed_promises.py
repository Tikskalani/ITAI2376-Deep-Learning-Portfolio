"""Promises Trilok owes are listed in every brief, one line each, never ranked away.

On 2026-10-22 the Opus brief dropped two Example Property Group promises (Brenda's
Rent Manager credits and the reply to Dan Roe) that were in its inputs.
The list is now appended by code. No model is called here.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import context_pack
from core.loop import briefs

BRENDA = "Remove the non-late-fee credits from the Rent Manager report (promised same day)."
CARTAS = "Kirkdale and Pine Court takeover follow-through after the Oct 7 email."
PACK = {
    "snapshot": "2026-10-08",
    "open_loops": {
        "demo-dental": [{"owner": "them", "who": "Dr. Lee", "since": "2026-10-05", "text": "Sign-off on the intake form."}],
        "property-group": [
            {"owner": "me", "who": "Brenda", "since": "2026-10-07", "text": BRENDA},
            {"owner": "me", "who": "Dan Roe", "since": "2026-10-07", "text": CARTAS},
        ],
        "sidework": [{"owner": "me", "who": "Trilok", "since": "2026-10-22", "text": "Promised today, not overdue yet."}],
    },
}


def test_overdue_promises_are_listed_one_line_each_and_nothing_else():
    block = context_pack.owes_block(PACK, today=date(2026, 10, 22))
    assert f"- Brenda (property-group, since Oct 7): {BRENDA}" in block
    assert f"- Dan Roe (property-group, since Oct 7): {CARTAS}" in block
    assert "Dr. Lee" not in block            # waiting on someone else
    assert "Promised today" not in block       # not overdue yet
    assert block.startswith("Promises you owe (2, from the open loops as of 2026-10-08):")


def test_a_stale_list_says_how_old_it_is():
    old = context_pack.owes_block(PACK, today=date(2026, 10, 22))
    assert "These open loops are 14 days old." in old
    fresh = context_pack.owes_block(PACK, today=date(2026, 10, 12))
    assert "days old" not in fresh
    header = context_pack.open_loops_block({**PACK}).splitlines()[0]
    assert header.startswith("Open loops as of 2026-10-08, ") and "days old" in header


def test_nothing_owed_and_fresh_is_silent():
    assert context_pack.owes_block({"snapshot": "2026-10-20", "open_loops": {}}, today=date(2026, 10, 22)) == ""


@pytest.fixture
def pack(monkeypatch):
    monkeypatch.setattr(context_pack, "load", lambda path=None: PACK)


async def test_a_full_brief_carries_them_even_when_the_model_leaves_them_out(pack):
    sent = AsyncMock(return_value=None)
    with (
        patch("core.loop.briefs._signals_context", new=AsyncMock(return_value="nothing new")),
        patch("core.loop.briefs._dispatch_with_retry",
              new=AsyncMock(return_value=SimpleNamespace(text="a tutoring platform first, then the connectors."))),
        patch("core.loop.briefs.notify", new=sent),
        patch("core.loop.briefs._store_brief", new=AsyncMock(return_value=None)),
        patch("core.loop.briefs.daily_health_line", new=AsyncMock(return_value="health")),
        patch("core.loop.briefs.daily_cost_line", new=AsyncMock(return_value="cost")),
        patch("core.loop.briefs.set_heartbeat", new=AsyncMock()),
    ):
        await briefs.run_brief("evening")
    body = sent.await_args.kwargs["body"]
    assert body.startswith("a tutoring platform first, then the connectors.")
    assert BRENDA in body and CARTAS in body


def test_the_ceo_is_told_not_to_repeat_them():
    assert "appended after your reply automatically" in briefs._brief_prompt("morning")


async def test_working_memory_lines_carry_the_local_date_and_time(monkeypatch):
    from datetime import UTC, datetime

    from core import agent_context

    row = {"summary": "Evening, Trilok.", "created_at": datetime(2026, 10, 22, 1, 1, tzinfo=UTC)}
    monkeypatch.setattr(agent_context, "recent_context", AsyncMock(return_value=[row]))
    block = await agent_context.recent_context_block("ceo")
    assert "[Oct 21 20:01] Evening, Trilok." in block  # 01:01 UTC is 20:01 the evening before in Houston
