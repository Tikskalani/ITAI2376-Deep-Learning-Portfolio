"""Unit tests for goal management.

Covers three layers, all mocked (no Postgres, no Telegram):
  * core/goals.py        -- the data layer, with core.goals.db patched
  * the CEO goal tools   -- create_goal / list_goals / close_goal handlers
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agents.ceo.tools import build_tools
from core import goals


def _goal_row(**over):
    """A goal row shaped like what core.goals selects."""
    base = {
        "id": uuid4(),
        "description": "a goal",
        "owner": "ceo",
        "status": "active",
        "success_criteria": None,
        "created_at": None,
        "target_date": None,
        "closed_at": None,
        "metadata": {},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# core/goals.py -- data layer
# ---------------------------------------------------------------------------


async def test_create_goal_returns_id_and_passes_description():
    gid = uuid4()
    with patch(
        "core.goals.db.fetchrow", new=AsyncMock(return_value={"id": gid})
    ) as fetchrow:
        result = await goals.create_goal("ship the thing")
    assert result == gid
    # fetchrow(sql, description, owner, success_criteria, target_date, metadata)
    assert fetchrow.await_args.args[1] == "ship the thing"
    assert fetchrow.await_args.args[2] == "ceo"


async def test_list_goals_active_filters_by_status():
    with patch(
        "core.goals.db.fetch", new=AsyncMock(return_value=[_goal_row()])
    ) as fetch:
        out = await goals.list_goals()
    assert len(out) == 1
    assert fetch.await_args.args[1] == "active"


async def test_list_goals_all_passes_no_status():
    with patch(
        "core.goals.db.fetch", new=AsyncMock(return_value=[])
    ) as fetch:
        await goals.list_goals(status=None)
    # status=None -> the query takes no parameter
    assert len(fetch.await_args.args) == 1


async def test_get_goal_found_and_missing():
    row = _goal_row()
    with patch("core.goals.db.fetchrow", new=AsyncMock(return_value=row)):
        assert (await goals.get_goal(row["id"]))["id"] == row["id"]
    with patch("core.goals.db.fetchrow", new=AsyncMock(return_value=None)):
        assert await goals.get_goal(uuid4()) is None


async def test_find_goal_by_full_uuid():
    gid = uuid4()
    row = _goal_row(id=gid)
    with patch("core.goals.db.fetchrow", new=AsyncMock(return_value=row)):
        out = await goals.find_goal(str(gid))
    assert out["id"] == gid


async def test_find_goal_rejects_short_prefix():
    # Too short to be a safe prefix, and not a UUID -> None, no DB call.
    assert await goals.find_goal("abc") is None


async def test_find_goal_ambiguous_prefix_returns_none():
    with patch(
        "core.goals.db.fetch",
        new=AsyncMock(return_value=[_goal_row(), _goal_row()]),
    ):
        assert await goals.find_goal("abcd1234") is None


async def test_close_goal_reports_row_change():
    with patch("core.goals.db.execute", new=AsyncMock(return_value="UPDATE 1")):
        assert await goals.close_goal(uuid4()) is True
    with patch("core.goals.db.execute", new=AsyncMock(return_value="UPDATE 0")):
        assert await goals.close_goal(uuid4()) is False


async def test_close_goal_rejects_invalid_status():
    with pytest.raises(ValueError):
        await goals.close_goal(uuid4(), status="active")


# ---------------------------------------------------------------------------
# CEO goal tools
# ---------------------------------------------------------------------------


def _ceo_tool(name: str):
    """Return one CEO tool handler by name."""
    return {t.name: t for t in build_tools()}[name].handler


def test_ceo_has_goal_tools():
    names = {t.name for t in build_tools()}
    assert {"create_goal", "list_goals", "close_goal"} <= names


async def test_create_goal_tool_creates():
    gid = uuid4()
    with patch(
        "agents.ceo.tools.create_goal", new=AsyncMock(return_value=gid)
    ) as create:
        out = await _ceo_tool("create_goal")({"description": "win the deal"})
    assert out["goal_id"] == str(gid)
    assert create.await_args.kwargs["description"] == "win the deal"


async def test_create_goal_tool_rejects_bad_date():
    out = await _ceo_tool("create_goal")(
        {"description": "win", "target_date": "next tuesday"}
    )
    assert "error" in out


async def test_create_goal_tool_accepts_iso_date():
    with patch(
        "agents.ceo.tools.create_goal", new=AsyncMock(return_value=uuid4())
    ) as create:
        out = await _ceo_tool("create_goal")(
            {"description": "win", "target_date": "2026-09-01"}
        )
    assert "error" not in out
    assert create.await_args.kwargs["target_date"] == date(2026, 9, 1)


async def test_create_goal_tool_requires_description():
    out = await _ceo_tool("create_goal")({"description": "   "})
    assert "error" in out


async def test_list_goals_tool():
    with patch(
        "agents.ceo.tools.list_goals",
        new=AsyncMock(return_value=[_goal_row(), _goal_row()]),
    ):
        out = await _ceo_tool("list_goals")({})
    assert out["count"] == 2
    assert len(out["goals"]) == 2


async def test_close_goal_tool():
    row = _goal_row()
    with (
        patch("agents.ceo.tools.find_goal", new=AsyncMock(return_value=row)),
        patch(
            "agents.ceo.tools.close_goal", new=AsyncMock(return_value=True)
        ) as close,
    ):
        out = await _ceo_tool("close_goal")({"goal_id": "abcd1234"})
    assert out["status"] == "closed"
    assert out["changed"] is True
    close.assert_awaited_once()


async def test_close_goal_tool_unknown_goal():
    with patch("agents.ceo.tools.find_goal", new=AsyncMock(return_value=None)):
        out = await _ceo_tool("close_goal")({"goal_id": "ffffffff"})
    assert "error" in out
