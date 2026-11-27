"""Unit tests for the Personal Ops agent.

The agent is a thin household-domain wrapper over the CFO's
recurring_bills and the COO's deadlines. We test the household filters,
registration, the toolset, and the household deadline writer.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from agents.personal_ops import register_personal_ops
from agents.personal_ops.tools import build_tools as po_build_tools
from core.runtime.dispatcher import get_registration


def _po_tool(name: str):
    return {t.name: t for t in po_build_tools()}[name].handler


def _bill_row(**over):
    row = {
        "id": uuid4(),
        "name": "ACA premium",
        "amount": 420.50,
        "cadence": "monthly",
        "due_day": 1,
        "account": None,
        "stream": "household",
        "status": "active",
        "last_seen": None,
        "notes": None,
    }
    row.update(over)
    return row


def _deadline_row(**over):
    row = {
        "id": uuid4(),
        "title": "ACA renewal",
        "due_date": date(2026, 11, 15),
        "domain": "household",
        "priority": "high",
        "status": "open",
        "notes": None,
        "trigger_id": None,
        "state": "due_soon",
        "days_until": 7,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_personal_ops_wires_tools():
    register_personal_ops()
    reg = get_registration("personal_ops")
    assert reg is not None
    assert reg.model_tier == "specialist"


def test_personal_ops_toolset():
    names = {t.name for t in po_build_tools()}
    assert names == {
        "list_household_bills",
        "household_deadlines",
        "add_household_deadline",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
    }


# ---------------------------------------------------------------------------
# list_household_bills
# ---------------------------------------------------------------------------


async def test_list_household_bills_filters_streams():
    rows = [
        _bill_row(stream="household", name="ACA"),
        _bill_row(stream="side_shop", name="Alibaba subscription"),
        _bill_row(stream=None, name="Spotify"),  # unstreamed counts
        _bill_row(stream="w2", name="Work-related"),
    ]
    with patch(
        "agents.personal_ops.tools.list_recurring_bills",
        new=AsyncMock(return_value=rows),
    ):
        out = await _po_tool("list_household_bills")({})
    names = {b["name"] for b in out["bills"]}
    assert names == {"ACA", "Spotify"}  # household + unstreamed only


async def test_list_household_bills_passes_status_filter():
    with patch(
        "agents.personal_ops.tools.list_recurring_bills",
        new=AsyncMock(return_value=[]),
    ) as lrb:
        await _po_tool("list_household_bills")({"status": "all"})
    assert lrb.call_args.kwargs["status"] is None


# ---------------------------------------------------------------------------
# household_deadlines
# ---------------------------------------------------------------------------


async def test_household_deadlines_filters_domain_and_splits_state():
    rows = [
        _deadline_row(domain="household", state="overdue", days_until=-2),
        _deadline_row(domain="auto_insurance", state="due_today", days_until=0),
        _deadline_row(domain="vehicle", state="overdue", days_until=-1),  # not household
        _deadline_row(domain="hcc", state="due_soon", days_until=3),  # not household
        _deadline_row(domain="aca", state="upcoming", days_until=10),
    ]
    with patch(
        "agents.personal_ops.tools.deadlines_needing_attention",
        new=AsyncMock(return_value=rows),
    ):
        out = await _po_tool("household_deadlines")({"horizon_days": 30})
    assert out["overdue_count"] == 1  # household one only
    assert len(out["due_today"]) == 1
    assert len(out["due_soon"]) == 1  # aca/upcoming


async def test_household_deadlines_clamps_horizon():
    with patch(
        "agents.personal_ops.tools.deadlines_needing_attention",
        new=AsyncMock(return_value=[]),
    ) as dna:
        await _po_tool("household_deadlines")({"horizon_days": 999})
    assert dna.call_args.kwargs["horizon_days"] == 120


# ---------------------------------------------------------------------------
# add_household_deadline
# ---------------------------------------------------------------------------


async def test_add_household_deadline_defaults_domain():
    did = uuid4()
    with patch(
        "agents.personal_ops.tools.add_deadline",
        new=AsyncMock(return_value=did),
    ) as ad:
        out = await _po_tool("add_household_deadline")(
            {"title": "ACA renewal", "due_date": "2026-11-15"}
        )
    assert out["deadline_id"] == str(did)
    assert out["domain"] == "household"
    assert ad.call_args.kwargs["domain"] == "household"
    assert ad.call_args.kwargs["source"] == "personal_ops"
    assert ad.call_args.kwargs["remind_days_before"] is None


async def test_add_household_deadline_with_reminder_and_subdomain():
    with patch(
        "agents.personal_ops.tools.add_deadline",
        new=AsyncMock(return_value=uuid4()),
    ) as ad:
        out = await _po_tool("add_household_deadline")(
            {
                "title": "Auto insurance renewal",
                "due_date": "2026-08-30",
                "domain": "auto_insurance",
                "remind_days_before": 14,
            }
        )
    assert out["reminder_set"] is True
    assert ad.call_args.kwargs["domain"] == "auto_insurance"
    assert ad.call_args.kwargs["remind_days_before"] == 14


async def test_add_household_deadline_requires_title_and_date():
    out = await _po_tool("add_household_deadline")({"title": ""})
    assert "error" in out
    out = await _po_tool("add_household_deadline")({"title": "X"})
    assert "error" in out
