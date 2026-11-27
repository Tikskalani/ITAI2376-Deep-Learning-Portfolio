"""Done on a promise in a brief, and open loops merged by person and text. No model call."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from core import context_pack as cp

MANUAL = {
    "snapshot": "2026-10-08",
    "open_loops": {"property-group": [
        {"owner": "me", "who": "Brenda", "since": "2026-10-07",
         "text": "Remove the non-late-fee credits from the Rent Manager report (promised same day)."},
        {"owner": "me", "who": "Dan Roe", "since": "2026-10-07",
         "text": "Kirkdale and Pine Court takeover follow-through after the Oct 7 email."},
    ]},
}
LEARNED = {
    "open_loops_at": "2026-10-20T06:40:00+00:00",
    "open_loops": {
        # The same Brenda loop, spelled with different spacing and case: one loop, not two.
        "property-group": [{"owner": "me", "who": "brenda", "since": "2026-10-07",
                              "text": "Remove the non-late-fee  credits from the Rent Manager report (promised same day)."}],
        "job-search": [{"owner": "me", "who": "Recruiter at Cursor", "since": "2026-10-18", "text": "Send availability."}],
    },
}


@pytest.fixture
def files(tmp_path, monkeypatch):
    manual, learned, closed = tmp_path / "context.yaml", tmp_path / "learned.yaml", tmp_path / "closed.yaml"
    manual.write_text(yaml.safe_dump(MANUAL), encoding="utf-8")
    learned.write_text(yaml.safe_dump(LEARNED), encoding="utf-8")
    monkeypatch.setattr(cp, "PATH", manual)
    monkeypatch.setattr(cp, "LEARNED_PATH", learned)
    monkeypatch.setattr(cp, "CLOSED_PATH", closed)
    monkeypatch.setattr(cp, "_cache", {"key": None, "data": {}})
    return closed


def _whos() -> list[str]:
    return [o["who"] for o in cp.owed()]


def test_merge_keeps_hand_written_loops_and_adds_learned_ones_once(files):
    data = cp.load()
    assert [it["who"] for it in data["open_loops"]["property-group"]] == ["Brenda", "Dan Roe"]
    assert data["open_loops"]["job-search"][0]["who"] == "Recruiter at Cursor"
    assert data["snapshot"] == "2026-10-20"


def test_done_closes_a_loop_for_good_even_after_mail_learning_rewrites(files):
    brenda = next(o for o in cp.owed() if o["who"] == "Brenda")
    closed = cp.close_loop(brenda["key"])
    assert closed is not None and closed["who"] == "Brenda" and files.exists()
    assert "Brenda" not in _whos() and "Dan Roe" in _whos()
    assert "Rent Manager" not in cp.owes_block()
    # Mail learning rewrites its file with the same loop again: it stays closed.
    cp.LEARNED_PATH.write_text(yaml.safe_dump({**LEARNED, "open_loops_at": "2026-10-21T06:40:00+00:00"}),
                               encoding="utf-8")
    assert "Brenda" not in _whos()
    assert cp.close_loop(brenda["key"]) is None  # already closed


async def test_the_brief_card_lists_each_promise_with_its_key(files):
    from core.loop import briefs

    sent = AsyncMock(return_value=None)
    with (
        patch("core.loop.briefs.notify", new=sent),
        patch("core.loop.briefs._store_brief", new=AsyncMock(return_value="00000000-0000-0000-0000-000000000001")),
    ):
        await briefs._deliver("Brief body.", "evening")
    owes = sent.await_args.kwargs["data"]["owes"]
    assert {o["who"] for o in owes} == {"Brenda", "Dan Roe", "Recruiter at Cursor"}
    assert all(len(o["key"]) == 12 for o in owes)


def test_the_close_route(files):
    async def fake_pool():
        class P:
            async def close(self): pass
        return P()

    with (
        patch("core.web.api.db.get_pool", new=fake_pool),
        patch("core.web.api.db.close_pool", new=AsyncMock()),
    ):
        from core.web.api import app
        with TestClient(app) as c:
            key = next(o["key"] for o in cp.owed() if o["who"] == "Dan Roe")
            assert c.post(f"/api/owes/{key}/close").json()["who"] == "Dan Roe"
            assert c.post(f"/api/owes/{key}/close").status_code == 404
            assert c.post("/api/owes/not-a-key!/close").status_code in (404, 422)
    assert "Dan Roe" not in _whos()
