"""Tests for core/loop/triage.py: parsing is pure; triage_new_signals runs
against the live test database with llm.complete replaced by a fake (the
reply is the structured-output object complete_json expects)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from core import db, projects, signals
from core.loop import triage
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Triage takes every new signal; real ones, their deadlines and the real
    # triage cost records must be neither triaged nor touched.
    async with livedb.shadow(monkeypatch, "signals", "deadlines", "llm_calls", "projects"):
        yield


async def test_parse_triage_tolerates_fences_prose_and_garbage():
    arr = [{"id": "a", "urgency": 2}, {"id": "b"}]
    assert triage.parse_triage(json.dumps(arr)) == arr
    assert triage.parse_triage("```json\n" + json.dumps(arr) + "\n```") == arr
    assert triage.parse_triage("Here you go:\n" + json.dumps(arr) + "\nDone.") == arr
    assert triage.parse_triage('[{"id": "a"}, 7, "x"]') == [{"id": "a"}]
    assert triage.parse_triage('{"id": "a"}') == []
    assert triage.parse_triage("") == []
    assert triage.parse_triage("no json") == []


def _sig(ext: str, **kw) -> signals.Signal:
    base = dict(source="test-triage", external_id=ext, occurred_at=datetime.now(UTC),
                subject=f"subject {ext}", body="body")
    base.update(kw)
    return signals.Signal(**base)


async def test_triage_applies_verdicts_deadlines_ignores_and_defaults(monkeypatch):
    portal = await projects.upsert_project("test-portal", "Portal", kind="client", priority=1)
    hcc = await projects.upsert_project("test-hcc", "HCC", kind="college")
    s_review = await signals.insert_signal(_sig("review", project_id=portal.id))
    s_noise = await signals.insert_signal(_sig("noise"))
    s_assign = await signals.insert_signal(_sig("assign", project_id=hcc.id,
                                                deadline=date.today() + timedelta(days=2)))
    s_skipped = await signals.insert_signal(_sig("skipped"))
    assert s_review and s_noise and s_assign and s_skipped

    captured: dict = {}

    async def fake_complete(**kw):
        captured.update(kw)
        return json.dumps({"verdicts": [
            {"id": str(s_review), "project": None, "urgency": 3, "importance": 3,
             "deadline": None, "gist": "Chris asked for a review on PR #7", "ignore": False},
            {"id": str(s_noise), "urgency": 0, "importance": 0, "gist": "bot chatter", "ignore": True},
            {"id": str(s_assign), "project": "test-hcc", "urgency": 2, "importance": 2,
             "deadline": "not-a-date", "gist": "Lab 3 is due Thursday"},
            {"id": "00000000-0000-0000-0000-000000000000", "urgency": 9},  # unknown id, dropped
        ]})

    monkeypatch.setattr(triage.llm, "complete", fake_complete)
    out = await triage.triage_new_signals(batch=10)
    assert out["error"] is None
    assert out["seen"] >= 4 and out["triaged"] >= 3 and out["ignored"] == 1
    assert out["interrupts"] == 1 and out["deadlines"] == 1

    # The call shape: Haiku tier, cached rubric with the project block, batch JSON.
    assert captured["tier"] == "scan" and captured["purpose"] == "triage"
    assert captured["cache_system"] is True
    assert captured["schema"] == triage.VERDICT_SCHEMA  # structured output, no regex hunting
    assert "test-portal: Portal" in captured["system"]
    assert str(s_review) in captured["user"]

    review = await signals.get_signal(s_review)
    assert review is not None
    assert review["urgency"] == 3 and review["importance"] == 3
    assert review["project_id"] == portal.id  # null from the model keeps the connector's link
    assert review["triage_notes"] == "Chris asked for a review on PR #7"
    assert review["triage_status"] == "triaged"

    noise = await signals.get_signal(s_noise)
    assert noise is not None and noise["triage_status"] == "ignored"

    assign = await signals.get_signal(s_assign)
    assert assign is not None
    assert assign["project_id"] == hcc.id
    assert assign["deadline"] == date.today() + timedelta(days=2)  # bad model date, signal's kept
    dl = await db.fetchrow("select * from deadlines where signal_id = $1", s_assign)
    assert dl is not None
    assert dl["title"] == "Lab 3 is due Thursday" and dl["priority"] == "high"
    assert dl["project_id"] == hcc.id and dl["domain"] == "test-hcc" and dl["source"] == "triage"

    skipped = await signals.get_signal(s_skipped)
    assert skipped is not None
    assert skipped["triage_status"] == "triaged" and skipped["triage_notes"] == "triage: no verdict returned"

    # Nothing left as new from this source; a second run is a no-op.
    assert [r for r in await signals.list_signals("new", limit=100) if r["source"] == "test-triage"] == []
    again = await triage.triage_new_signals(batch=10)
    assert again["seen"] == 0 or all(r["source"] != "test-triage" for r in await signals.list_signals("new", limit=100))


async def test_triage_leaves_batch_new_when_model_fails(monkeypatch):
    sid = await signals.insert_signal(_sig("retry"))
    assert sid

    async def empty(**kw):
        return ""

    monkeypatch.setattr(triage.llm, "complete", empty)
    out = await triage.triage_new_signals(batch=10)
    assert out["error"] == "no verdicts" and out["triaged"] == 0
    row = await signals.get_signal(sid)
    assert row is not None and row["triage_status"] == "new"


async def test_deadline_created_once_per_signal(monkeypatch):
    p = await projects.upsert_project("test-once", "Once")
    sid = await signals.insert_signal(_sig("once", project_id=p.id))
    assert sid
    row = await signals.get_signal(sid)
    assert row is not None
    due = date.today() + timedelta(days=5)
    await triage._ensure_deadline(row, due, "do the thing", "test-once", p.id, 2)
    await triage._ensure_deadline(row, due, "do the thing", "test-once", p.id, 2)
    n = await db.fetchval("select count(*) from deadlines where signal_id = $1", sid)
    assert n == 1
