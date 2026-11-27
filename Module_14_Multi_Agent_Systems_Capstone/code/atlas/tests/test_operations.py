"""Unit tests for the COO operations layer and the COO agent tools.

The pure deadline classifier is tested directly. Tool handlers are tested
with the database functions mocked, so no Postgres is touched.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agents.coo import register_coo
from agents.coo.tools import build_tools as coo_build_tools
from core.operations import classify_deadline
from core.runtime.dispatcher import get_registration


def _coo_tool(name: str):
    return {t.name: t for t in coo_build_tools()}[name].handler


def _deadline_row(**over):
    row = {
        "id": uuid4(),
        "title": "Renew vehicle registration",
        "due_date": date(2026, 6, 1),
        "domain": "vehicle",
        "priority": "high",
        "status": "open",
        "notes": None,
        "trigger_id": None,
    }
    row.update(over)
    return row


def _template_row(**over):
    row = {
        "id": uuid4(),
        "name": "Monthly statement reconciliation",
        "cadence": "monthly",
        "domain": "money",
        "lead_time_days": 3,
        "checklist": None,
        "status": "active",
        "last_run": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_coo_wires_tools():
    register_coo()
    reg = get_registration("coo")
    assert reg is not None
    assert reg.model_tier == "specialist"
    assert reg.tools_factory is not None
    names = {t.name for t in reg.tools_factory()}
    assert "add_deadline" in names
    assert "check_slipping" in names


def test_coo_toolset():
    names = {t.name for t in coo_build_tools()}
    assert names == {
        "add_deadline",
        "list_deadlines",
        "complete_deadline",
        "check_slipping",
        "upcoming_events",
        "calendar_create_event",
        "add_task_template",
        "list_task_templates",
        "update_task_template",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
    }


# ---------------------------------------------------------------------------
# classify_deadline
# ---------------------------------------------------------------------------


def test_classify_deadline_states():
    today = date(2026, 5, 23)
    assert classify_deadline(date(2026, 5, 20), today) == ("overdue", -3)
    assert classify_deadline(date(2026, 5, 23), today) == ("due_today", 0)
    assert classify_deadline(date(2026, 5, 25), today) == ("due_soon", 2)
    assert classify_deadline(date(2026, 6, 20), today) == ("upcoming", 28)


# ---------------------------------------------------------------------------
# add_deadline
# ---------------------------------------------------------------------------


async def test_add_deadline_with_reminder():
    did = uuid4()
    with patch(
        "agents.coo.tools.add_deadline", new=AsyncMock(return_value=did)
    ) as ad:
        out = await _coo_tool("add_deadline")(
            {
                "title": "Renew vehicle registration",
                "due_date": "2026-06-01",
                "domain": "vehicle",
                "priority": "high",
                "remind_days_before": 7,
            }
        )
    assert out["deadline_id"] == str(did)
    assert out["reminder_set"] is True
    assert ad.call_args.kwargs["remind_days_before"] == 7
    assert ad.call_args.kwargs["due_date"] == date(2026, 6, 1)


async def test_add_deadline_without_reminder():
    with patch(
        "agents.coo.tools.add_deadline", new=AsyncMock(return_value=uuid4())
    ) as ad:
        out = await _coo_tool("add_deadline")(
            {"title": "Pay AEP bill", "due_date": "2026-06-15"}
        )
    assert out["reminder_set"] is False
    assert ad.call_args.kwargs["remind_days_before"] is None


async def test_add_deadline_requires_title():
    out = await _coo_tool("add_deadline")({"due_date": "2026-06-01"})
    assert "error" in out


async def test_add_deadline_rejects_bad_date():
    out = await _coo_tool("add_deadline")(
        {"title": "X", "due_date": "June 1st"}
    )
    assert "error" in out


async def test_add_deadline_rejects_bad_priority():
    out = await _coo_tool("add_deadline")(
        {"title": "X", "due_date": "2026-06-01", "priority": "urgent"}
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# list_deadlines and complete_deadline
# ---------------------------------------------------------------------------


async def test_list_deadlines():
    with patch(
        "agents.coo.tools.list_deadlines",
        new=AsyncMock(return_value=[_deadline_row(), _deadline_row()]),
    ):
        out = await _coo_tool("list_deadlines")({"domain": "vehicle"})
    assert out["count"] == 2
    assert out["deadlines"][0]["title"] == "Renew vehicle registration"


async def test_complete_deadline():
    row = _deadline_row()
    with patch(
        "agents.coo.tools.find_deadline", new=AsyncMock(return_value=row)
    ), patch(
        "agents.coo.tools.complete_deadline",
        new=AsyncMock(return_value=True),
    ) as cd:
        out = await _coo_tool("complete_deadline")(
            {"deadline_id": str(row["id"]), "status": "done"}
        )
    assert out["changed"] is True
    assert cd.call_args.kwargs["status"] == "done"


async def test_complete_deadline_unknown_id():
    with patch(
        "agents.coo.tools.find_deadline", new=AsyncMock(return_value=None)
    ):
        out = await _coo_tool("complete_deadline")(
            {"deadline_id": "abcd1234"}
        )
    assert "error" in out


async def test_complete_deadline_rejects_bad_status():
    with patch(
        "agents.coo.tools.find_deadline",
        new=AsyncMock(return_value=_deadline_row()),
    ):
        out = await _coo_tool("complete_deadline")(
            {"deadline_id": "abcd1234", "status": "finished"}
        )
    assert "error" in out


# ---------------------------------------------------------------------------
# check_slipping
# ---------------------------------------------------------------------------


async def test_check_slipping_splits_by_state():
    rows = [
        _deadline_row(state="overdue", days_until=-2),
        _deadline_row(state="due_today", days_until=0),
        _deadline_row(state="due_soon", days_until=2),
        _deadline_row(state="upcoming", days_until=5),
    ]
    with patch(
        "agents.coo.tools.deadlines_needing_attention",
        new=AsyncMock(return_value=rows),
    ):
        out = await _coo_tool("check_slipping")({"horizon_days": 7})
    assert out["overdue_count"] == 1
    assert len(out["overdue"]) == 1
    assert len(out["due_today"]) == 1
    assert len(out["due_soon"]) == 2  # due_soon + upcoming


async def test_check_slipping_clamps_horizon():
    with patch(
        "agents.coo.tools.deadlines_needing_attention",
        new=AsyncMock(return_value=[]),
    ) as dna:
        await _coo_tool("check_slipping")({"horizon_days": 999})
    assert dna.call_args.kwargs["horizon_days"] == 90


# ---------------------------------------------------------------------------
# task templates
# ---------------------------------------------------------------------------


async def test_add_task_template():
    tid = uuid4()
    with patch(
        "agents.coo.tools.add_task_template",
        new=AsyncMock(return_value=tid),
    ) as at:
        out = await _coo_tool("add_task_template")(
            {
                "name": "Quarterly tax estimate",
                "cadence": "quarterly",
                "lead_time_days": 14,
            }
        )
    assert out["template_id"] == str(tid)
    assert at.call_args.kwargs["lead_time_days"] == 14


async def test_add_task_template_rejects_bad_cadence():
    out = await _coo_tool("add_task_template")(
        {"name": "X", "cadence": "biweekly"}
    )
    assert "error" in out


async def test_add_task_template_requires_name():
    out = await _coo_tool("add_task_template")({"cadence": "monthly"})
    assert "error" in out


async def test_list_task_templates():
    with patch(
        "agents.coo.tools.list_task_templates",
        new=AsyncMock(return_value=[_template_row()]),
    ):
        out = await _coo_tool("list_task_templates")({})
    assert out["count"] == 1
    assert out["templates"][0]["cadence"] == "monthly"


async def test_update_task_template():
    row = _template_row()
    with patch(
        "agents.coo.tools.find_task_template",
        new=AsyncMock(return_value=row),
    ), patch(
        "agents.coo.tools.update_task_template",
        new=AsyncMock(return_value=True),
    ) as up:
        out = await _coo_tool("update_task_template")(
            {"template_id": str(row["id"]), "status": "paused"}
        )
    assert out["changed"] is True
    assert out["updated_fields"] == ["status"]
    assert up.call_args.kwargs["status"] == "paused"


async def test_update_task_template_unknown_id():
    with patch(
        "agents.coo.tools.find_task_template",
        new=AsyncMock(return_value=None),
    ):
        out = await _coo_tool("update_task_template")(
            {"template_id": "abcd1234", "status": "paused"}
        )
    assert "error" in out


async def test_update_task_template_needs_a_field():
    with patch(
        "agents.coo.tools.find_task_template",
        new=AsyncMock(return_value=_template_row()),
    ):
        out = await _coo_tool("update_task_template")(
            {"template_id": "abcd1234"}
        )
    assert "error" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
