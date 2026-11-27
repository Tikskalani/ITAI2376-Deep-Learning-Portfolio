"""Unit tests for the Sunday decision-review loop.

Covers core/decisions_review.py and its wiring into briefs and the
Telegram surface. Everything external is mocked: no Postgres, no Ollama,
no Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core import decisions_review as dr


def _decision_row(**over):
    base = {
        "id": uuid4(),
        "summary": "picked supplier A",
        "rationale": "best comms score",
        "expected_outcome": "samples in two weeks",
        "made_by": "owner",
        "domain": "side_shop",
        "status": "open",
        "outcome": None,
        "assessment": None,
        "decided_at": datetime.now(UTC) - timedelta(days=2),
        "review_at": None,
        "resolved_at": None,
        "goal_id": None,
        "episode_id": None,
        "metadata": {},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# list_reviewable
# ---------------------------------------------------------------------------


async def test_list_reviewable_queries_open_with_cutoff():
    with patch(
        "core.decisions_review.db.fetch",
        new=AsyncMock(return_value=[_decision_row(), _decision_row()]),
    ) as fetch:
        out = await dr.list_reviewable(min_age_hours=24)
    assert len(out) == 2
    sql = fetch.await_args.args[0]
    assert "status = 'open'" in sql
    assert "order by decided_at asc" in sql
    # The SQL must encode both review_at and decided_at gates.
    assert "review_at" in sql
    assert "decided_at" in sql
    # The cutoff arg is a tz-aware datetime roughly 24h before now.
    cutoff = fetch.await_args.args[1]
    delta = datetime.now(UTC) - cutoff
    assert timedelta(hours=23) < delta < timedelta(hours=25)


async def test_list_reviewable_sql_gates_future_review_at():
    """The query must split into (review_at due) OR (no review_at and old)."""
    with patch(
        "core.decisions_review.db.fetch", new=AsyncMock(return_value=[])
    ) as fetch:
        await dr.list_reviewable()
    sql = fetch.await_args.args[0]
    # Anchored substrings; exact whitespace is asserted via the literal we wrote.
    assert "review_at is not null and review_at <= current_date" in sql
    assert "review_at is null and decided_at <= $1" in sql


async def test_list_reviewable_honors_custom_age_threshold():
    with patch(
        "core.decisions_review.db.fetch", new=AsyncMock(return_value=[])
    ) as fetch:
        await dr.list_reviewable(min_age_hours=72)
    cutoff = fetch.await_args.args[1]
    delta = datetime.now(UTC) - cutoff
    assert timedelta(hours=71) < delta < timedelta(hours=73)


# ---------------------------------------------------------------------------
# _lesson_for content templates
# ---------------------------------------------------------------------------


def test_lesson_for_right_validates():
    row = _decision_row()
    content = dr._lesson_for(row, "right")
    assert "Validated" in content
    assert row["rationale"] in content
    assert "—" not in content  # no em-dash, voice rule


def test_lesson_for_wrong_has_correction_prefix():
    row = _decision_row()
    content = dr._lesson_for(row, "wrong")
    assert content.startswith("correction:")
    assert "did not hold up" in content


def test_lesson_for_mixed_says_partial():
    row = _decision_row()
    content = dr._lesson_for(row, "mixed")
    assert content.startswith("Partial:")


def test_lesson_for_missing_rationale_uses_fallback():
    row = _decision_row(rationale=None)
    content = dr._lesson_for(row, "wrong")
    assert "no rationale recorded" in content


# ---------------------------------------------------------------------------
# record_review
# ---------------------------------------------------------------------------


async def test_record_review_rejects_bad_assessment():
    with pytest.raises(ValueError):
        await dr.record_review(uuid4(), "great")


async def test_record_review_happy_path_writes_lesson():
    row = _decision_row()
    lesson_id = uuid4()
    with (
        patch(
            "core.decisions_review.resolve_decision",
            new=AsyncMock(return_value=True),
        ) as resolve,
        patch(
            "core.decisions_review.db.fetchrow",
            new=AsyncMock(return_value=row),
        ),
        patch(
            "core.decisions_review.record_lesson",
            new=AsyncMock(return_value=lesson_id),
        ) as lesson,
    ):
        ok = await dr.record_review(row["id"], "right")
    assert ok is True
    resolve.assert_awaited_once()
    lesson.assert_awaited_once()
    kwargs = lesson.await_args.kwargs
    assert kwargs["domain"] == "side_shop"
    assert kwargs["metadata"]["assessment"] == "right"
    assert kwargs["metadata"]["correction"] is False
    assert kwargs["metadata"]["source"] == "weekly_review"
    assert kwargs["metadata"]["decision_id"] == str(row["id"])


async def test_record_review_wrong_marks_correction_true():
    row = _decision_row()
    with (
        patch(
            "core.decisions_review.resolve_decision",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core.decisions_review.db.fetchrow",
            new=AsyncMock(return_value=row),
        ),
        patch(
            "core.decisions_review.record_lesson",
            new=AsyncMock(return_value=uuid4()),
        ) as lesson,
    ):
        await dr.record_review(row["id"], "wrong")
    metadata = lesson.await_args.kwargs["metadata"]
    assert metadata["correction"] is True
    content = lesson.await_args.kwargs["content"]
    assert content.startswith("correction:")


async def test_record_review_idempotent_when_already_resolved():
    """resolve_decision returns False the second time; no lesson written."""
    with (
        patch(
            "core.decisions_review.resolve_decision",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core.decisions_review.record_lesson", new=AsyncMock()
        ) as lesson,
    ):
        ok = await dr.record_review(uuid4(), "right")
    assert ok is False
    lesson.assert_not_awaited()


async def test_record_review_falls_back_when_decision_missing():
    """If the resolved decision somehow can't be re-read, still records
    a lesson using empty defaults (defensive)."""
    with (
        patch(
            "core.decisions_review.resolve_decision",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core.decisions_review.db.fetchrow",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.decisions_review.record_lesson",
            new=AsyncMock(return_value=uuid4()),
        ) as lesson,
    ):
        ok = await dr.record_review(uuid4(), "mixed")
    assert ok is True
    kwargs = lesson.await_args.kwargs
    # No domain on the row, falls back to 'decision-review'.
    assert kwargs["domain"] == "decision-review"


# ---------------------------------------------------------------------------
# review_prompt_text
# ---------------------------------------------------------------------------


def test_review_prompt_text_full_decision():
    row = _decision_row()
    text = dr.review_prompt_text(row)
    assert row["summary"] in text
    assert row["rationale"] in text
    assert row["expected_outcome"] in text
    assert "How did this call hold up?" in text


def test_review_prompt_text_minimal_decision():
    row = _decision_row(rationale=None, expected_outcome=None)
    text = dr.review_prompt_text(row)
    assert row["summary"] in text
    assert "Why:" not in text
    assert "Expected:" not in text


# ---------------------------------------------------------------------------
# The review round goes out as one notification
# ---------------------------------------------------------------------------


async def test_send_review_round_sends_one_notification_with_ids():
    rows = [_decision_row(), _decision_row(), _decision_row()]
    with (
        patch("core.decisions_review.list_reviewable", new=AsyncMock(return_value=rows)),
        patch("core.surfaces.notify.notify", new=AsyncMock(return_value=SimpleNamespace(notification_id=uuid4()))) as note,
    ):
        sent = await dr.send_review_round()
    assert sent == 3
    note.assert_awaited_once()
    kw = note.await_args.kwargs
    assert kw["kind"] == "decision_review" and kw["title"] == "3 decisions to review"
    assert [d["id"] for d in kw["data"]["decisions"]] == [str(r["id"]) for r in rows]
    assert rows[0]["summary"] in kw["body"]


async def test_send_review_round_returns_zero_when_no_decisions():
    with (
        patch("core.decisions_review.list_reviewable", new=AsyncMock(return_value=[])),
        patch("core.surfaces.notify.notify", new=AsyncMock()) as note,
    ):
        assert await dr.send_review_round() == 0
    note.assert_not_awaited()


async def test_send_review_round_survives_notify_failure():
    with (
        patch("core.decisions_review.list_reviewable", new=AsyncMock(return_value=[_decision_row()])),
        patch("core.surfaces.notify.notify", new=AsyncMock(side_effect=RuntimeError("down"))),
    ):
        assert await dr.send_review_round() == 0
    with (
        patch("core.decisions_review.list_reviewable", new=AsyncMock(return_value=[_decision_row()])),
        patch("core.surfaces.notify.notify", new=AsyncMock(return_value=None)),
    ):
        assert await dr.send_review_round() == 0
