"""Unit tests for the episode replay layer.

db.fetch / db.fetchrow are mocked so no live Postgres is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.replay import (
    find_episode,
    format_replay,
    replay_record,
)


def _ep_row(**over) -> dict:
    row = {
        "id": uuid4(),
        "started_at": datetime(2026, 5, 26, 16, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 5, 26, 16, 0, 16, tzinfo=UTC),
        "agent_name": "cfo",
        "participants": ["owner", "cfo"],
        "summary": "Imported May 2026 statement for chase_checking",
        "outcome": "31 transactions, net +$1460.66",
        "what_changed": {
            "tokens": {"input_tokens": 1245, "output_tokens": 312},
            "turns": 3,
        },
        "transcript_path": None,
        "tags": [],
    }
    row.update(over)
    return row


def _action_row(**over) -> dict:
    row = {
        "id": uuid4(),
        "agent_name": "cfo",
        "action_type": "tool_call",
        "inputs": {"name": "import_statement", "path": "/tmp/x.csv"},
        "outputs": {"imported": True, "row_count": 31},
        "approved_by": "auto",
        "episode_id": uuid4(),
        "goal_id": None,
        "created_at": datetime(2026, 5, 26, 16, 0, 5, tzinfo=UTC),
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# find_episode
# ---------------------------------------------------------------------------


async def test_find_episode_by_full_uuid():
    eid = uuid4()
    row = _ep_row(id=eid)
    with patch("core.replay.db.fetchrow", new=AsyncMock(return_value=row)):
        found = await find_episode(str(eid))
    assert found["id"] == eid


async def test_find_episode_by_prefix():
    eid = UUID("12345678-1234-5678-1234-567812345678")
    row = _ep_row(id=eid)
    with patch("core.replay.db.fetch", new=AsyncMock(return_value=[row])):
        found = await find_episode("12345678")
    assert found["id"] == eid


async def test_find_episode_short_prefix_returns_none():
    """Refuse to guess from very short prefixes that could match many rows."""
    found = await find_episode("ab")
    assert found is None


async def test_find_episode_ambiguous_prefix_returns_none():
    """If a prefix matches more than one episode, refuse to pick."""
    with patch(
        "core.replay.db.fetch",
        new=AsyncMock(return_value=[_ep_row(), _ep_row()]),
    ):
        found = await find_episode("abcdef12")
    assert found is None


# ---------------------------------------------------------------------------
# replay_record
# ---------------------------------------------------------------------------


async def test_replay_record_returns_episode_with_actions():
    row = _ep_row()
    actions = [_action_row(), _action_row(action_type="dispatch")]
    with patch(
        "core.replay.find_episode", new=AsyncMock(return_value=row)
    ), patch(
        "core.replay.episode_actions", new=AsyncMock(return_value=actions)
    ):
        rec = await replay_record(str(row["id"]))
    assert rec["episode"] is row
    assert rec["actions"] == actions


async def test_replay_record_missing_episode_returns_none():
    with patch("core.replay.find_episode", new=AsyncMock(return_value=None)):
        assert await replay_record("12345678") is None


# ---------------------------------------------------------------------------
# format_replay
# ---------------------------------------------------------------------------


def test_format_replay_renders_full_episode():
    row = _ep_row()
    actions = [_action_row()]
    out = format_replay({"episode": row, "actions": actions})
    assert "Episode" in out
    assert "cfo" in out
    assert "Imported May 2026 statement" in out
    assert "Actions (1):" in out
    assert "import_statement" in out


def test_format_replay_handles_empty_actions():
    row = _ep_row()
    out = format_replay({"episode": row, "actions": []})
    assert "(no tool calls recorded under this episode)" in out


def test_format_replay_truncates_long_payloads():
    row = _ep_row()
    big = {"x": "z" * 1000}
    a = _action_row(outputs=big)
    out = format_replay({"episode": row, "actions": [a]}, detail_chars=50)
    # The hellish wall of z's must be truncated, with an ellipsis at the end.
    long_lines = [line for line in out.splitlines() if line.startswith("     outputs:")]
    assert long_lines
    assert "…" in long_lines[0]


def test_format_replay_shows_duration():
    row = _ep_row(
        started_at=datetime(2026, 5, 26, 16, 0, tzinfo=UTC),
        ended_at=datetime(2026, 5, 26, 16, 0, 16, tzinfo=UTC),
    )
    out = format_replay({"episode": row, "actions": []})
    assert "16.0s" in out


def test_format_replay_handles_open_episode():
    row = _ep_row(ended_at=None)
    out = format_replay({"episode": row, "actions": []})
    assert "still open" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
