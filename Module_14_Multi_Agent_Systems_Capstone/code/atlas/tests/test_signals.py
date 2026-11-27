"""Tests for core/signals.py and core/connectors/state.py on the live DB.

Rows use the 'test-' source prefix and are removed afterwards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from core import db, projects, signals
from core.connectors import state

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup():
    yield
    await db.execute("delete from signals where source like 'test-%'")
    await db.execute("delete from connector_state where source like 'test-%'")
    await db.execute("delete from projects where slug like 'test-%'")


def _sig(ext: str, **kw) -> signals.Signal:
    base = dict(
        source="test-github",
        external_id=ext,
        occurred_at=datetime.now(UTC),
        subject=f"event {ext}",
        body="something happened",
    )
    base.update(kw)
    return signals.Signal(**base)


async def test_insert_is_idempotent_on_source_and_external_id():
    first = await signals.insert_signal(_sig("e1"))
    assert first is not None
    assert await signals.insert_signal(_sig("e1", subject="changed")) is None
    assert await signals.insert_signals([_sig("e1"), _sig("e2"), _sig("e3")]) == 2
    row = await signals.get_signal(first)
    assert row is not None and row["subject"] == "event e1"
    assert row["triage_status"] == "new"


async def test_insert_with_project_bumps_last_signal_at():
    p = await projects.upsert_project("test-proj", "Proj")
    when = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
    await signals.insert_signal(_sig("linked", project_id=p.id, occurred_at=when))
    again = await projects.get_project("test-proj")
    assert again is not None and again.last_signal_at == when
    # An older signal does not move the timestamp backwards.
    await signals.insert_signal(
        _sig("older", project_id=p.id, occurred_at=when - timedelta(days=1))
    )
    again = await projects.get_project("test-proj")
    assert again is not None and again.last_signal_at == when


async def test_triage_updates_and_listing_filters():
    sid = await signals.insert_signal(_sig("t1"))
    assert sid is not None
    await signals.set_triage(
        sid, urgency=7, importance=-1, notes="PR waits on review",
        deadline=date(2026, 10, 9),
    )
    row = await signals.get_signal(sid)
    assert row is not None
    assert row["urgency"] == 3 and row["importance"] == 0  # clamped
    assert row["triage_status"] == "triaged"
    assert row["deadline"] == date(2026, 10, 9)
    assert row["triaged_at"] is not None

    assert [r["id"] for r in await signals.list_signals("new") if r["source"] == "test-github"] == []
    triaged = [r for r in await signals.list_signals("triaged", min_urgency=3) if r["source"] == "test-github"]
    assert [r["id"] for r in triaged] == [sid]

    assert await signals.set_status([sid], "actioned") == 1
    row = await signals.get_signal(sid)
    assert row is not None and row["triage_status"] == "actioned"
    with pytest.raises(ValueError):
        await signals.set_status([sid], "nonsense")


async def test_brief_block_orders_by_importance_and_skips_new_and_ignored():
    p = await projects.upsert_project("test-brief", "Brief")
    a = await signals.insert_signal(_sig("a", project_id=p.id))
    b = await signals.insert_signal(_sig("b", project_id=p.id))
    c = await signals.insert_signal(_sig("c"))
    await signals.insert_signal(_sig("d"))  # stays new
    assert a and b and c
    await signals.set_triage(a, urgency=1, importance=1, notes="minor")
    await signals.set_triage(b, urgency=3, importance=3, notes="deploy failed", deadline=date(2026, 10, 8))
    await signals.set_triage(c, urgency=0, importance=0, status="ignored")
    rows = [r for r in await signals.signals_for_brief(24) if r["source"] == "test-github"]
    assert [r["id"] for r in rows] == [b, a]
    block = signals.format_brief_block(rows)
    assert block.splitlines()[0].startswith("- [test-brief] ")
    assert "!!deploy failed due 2026-10-08" in block
    assert signals.format_brief_block([]) == "Nothing new arrived."


async def test_connector_state_cursor_and_failure_streak():
    assert await state.get_cursor("test-src") == {}
    assert state.stale(await state.get_state("test-src"), max_age_minutes=5)

    await state.set_cursor("test-src", {"last_id": "42"})
    s = await state.get_state("test-src")
    assert s["cursor"] == {"last_id": "42"}
    assert s["consecutive_failures"] == 0
    assert not state.stale(s, max_age_minutes=5)

    assert await state.record_failure("test-src", "boom") == 1
    assert await state.record_failure("test-src", "boom again") == 2
    s = await state.get_state("test-src")
    assert s["last_error"] == "boom again"
    assert s["cursor"] == {"last_id": "42"}  # a failure keeps the cursor

    await state.record_ok("test-src")
    s = await state.get_state("test-src")
    assert s["consecutive_failures"] == 0 and s["last_error"] is None
