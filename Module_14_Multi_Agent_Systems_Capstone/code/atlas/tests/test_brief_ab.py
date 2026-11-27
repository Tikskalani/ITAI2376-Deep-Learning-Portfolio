"""scripts/brief_ab.py: the side-by-side writes nothing and respects its cap. No model call."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts import brief_ab


async def test_writing_tools_are_stubbed_and_reading_tools_are_real():
    cfg = brief_ab._ceo_config()
    by_name = {t.name: t for t in cfg.tools}
    assert brief_ab.READ_ONLY <= set(by_name)
    out = await by_name["remember_fact"].handler({"content": "x"})
    assert out["ok"] is True and "test run" in out["note"]
    for name in ("add_task", "dispatch_agent", "log_decision", "queue_for_tonight"):
        assert "test run" in (await by_name[name].handler({}))["note"]


async def test_the_cap_stops_the_second_run(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_ab, "REPO_ROOT", tmp_path)
    expensive = SimpleNamespace(text="Brief.", usage={"cache_creation_input_tokens": 300_000}, turns=3)
    run = AsyncMock(return_value=expensive)  # 300k cache-write tokens of Opus = $1.50
    with (
        patch.object(brief_ab.rt, "run_agent", new=run),
        patch.object(brief_ab.briefs, "_signals_context", new=AsyncMock(return_value="nothing new")),
        patch.object(brief_ab.db, "close_pool", new=AsyncMock()),
    ):
        await brief_ab.main("evening", Decimal("1.50"))
    assert run.await_count == 1
    written = list((tmp_path / "data" / "brief_ab").iterdir())[0]
    assert "## A" in (written / "briefs.md").read_text(encoding="utf-8")


def test_an_evening_test_after_midnight_is_labeled_with_the_evening_before():
    from datetime import UTC, date, datetime

    assert brief_ab.slot_day("evening", datetime(2026, 10, 22, 6, 15, tzinfo=UTC)) == date(2026, 10, 21)  # 01:15 Houston
    assert brief_ab.slot_day("evening", datetime(2026, 10, 22, 1, 30, tzinfo=UTC)) == date(2026, 10, 21)  # 20:30 Houston
    assert brief_ab.slot_day("morning", datetime(2026, 10, 22, 6, 15, tzinfo=UTC)) == date(2026, 10, 22)


def test_the_prompt_and_the_today_line_follow_the_pinned_day():
    from datetime import date

    with brief_ab._pinned(date(2026, 10, 21)):
        assert "Wednesday, October 21, 2026" in brief_ab.briefs._brief_prompt("evening")
        assert brief_ab.rt.date.today() == date(2026, 10, 21)
    assert brief_ab.rt.date is date  # the pin is lifted afterwards
