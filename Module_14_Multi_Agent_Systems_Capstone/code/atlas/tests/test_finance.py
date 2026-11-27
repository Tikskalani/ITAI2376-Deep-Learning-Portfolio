"""Unit tests for the CFO finance layer and the CFO agent tools.

Pure functions (anomaly math, month arithmetic, file hashing) are tested
directly. Tool handlers are tested with the database functions mocked, so
no Postgres or Ollama is touched. The one exception is import_statement,
which parses a real temporary CSV through the statement connector while
the three database writes are mocked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import core.finance as finance
from agents.cfo import register_cfo
from agents.cfo.tools import build_tools as cfo_build_tools
from core.runtime.dispatcher import get_registration


def _cfo_tool(name: str):
    return {t.name: t for t in cfo_build_tools()}[name].handler


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_cfo_wires_tools():
    register_cfo()
    reg = get_registration("cfo")
    assert reg is not None
    assert reg.model_tier == "specialist"
    assert reg.tools_factory is not None
    names = {t.name for t in reg.tools_factory()}
    assert "import_statement" in names
    assert "cash_flow" in names
    assert "detect_anomalies" in names


def test_cfo_toolset():
    names = {t.name for t in cfo_build_tools()}
    assert names == {
        "import_statement",
        "portfolio",
        "cash_flow",
        "list_transactions",
        "categorize",
        "add_recurring_bill",
        "list_recurring_bills",
        "update_recurring_bill",
        "detect_anomalies",
        "log_decision",
        "recall_facts",
        "remember_fact",
        "recall_lessons",
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_month_range():
    assert finance.month_range(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert finance.month_range(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert finance.month_range(2026, 12) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )


def test_month_range_rejects_bad_month():
    import pytest

    with pytest.raises(ValueError):
        finance.month_range(2026, 13)


def test_shift_month():
    assert finance._shift_month(2026, 1, -3) == (2025, 10)
    assert finance._shift_month(2026, 5, -3) == (2026, 2)
    assert finance._shift_month(2026, 12, 1) == (2027, 1)


def test_hash_file_is_deterministic(tmp_path: Path):
    f = tmp_path / "stmt.csv"
    f.write_text("Date,Description,Amount\n2026-01-01,X,-1.00\n")
    h1 = finance.hash_file(str(f))
    h2 = finance.hash_file(str(f))
    assert h1 == h2 and len(h1) == 64

    f.write_text("Date,Description,Amount\n2026-01-01,Y,-2.00\n")
    assert finance.hash_file(str(f)) != h1  # content change -> new hash


def test_category_swings_flags_spike_and_new():
    current = {
        "groceries": Decimal("300"),
        "fuel": Decimal("80"),
        "newcat": Decimal("150"),
        "tiny": Decimal("5"),
    }
    trailing = {"groceries": Decimal("180"), "fuel": Decimal("75")}
    flags = finance._category_swings(current, trailing)
    kinds = {f["category"]: f["kind"] for f in flags}
    assert kinds == {"groceries": "spike", "newcat": "new"}
    # fuel rose only slightly; tiny new category is below the material bar.


def test_category_swings_respects_absolute_floor():
    # A 3x jump, but only a few dollars in absolute terms: not flagged.
    flags = finance._category_swings(
        {"snacks": Decimal("12")}, {"snacks": Decimal("4")}
    )
    assert flags == []


def test_large_transactions_flags_outliers():
    outflows = [
        {"description": "rent", "amount": Decimal("-1800"), "posted": date(2026, 2, 1)},
        {"description": "coffee", "amount": Decimal("-5"), "posted": date(2026, 2, 2)},
        {"description": "tv", "amount": Decimal("-600"), "posted": date(2026, 2, 3)},
        {"description": "lunch", "amount": Decimal("-12"), "posted": date(2026, 2, 4)},
    ]
    flagged = finance._large_transactions(outflows)
    assert [f["description"] for f in flagged] == ["rent"]


def test_large_transactions_empty():
    assert finance._large_transactions([]) == []


# ---------------------------------------------------------------------------
# import_statement
# ---------------------------------------------------------------------------


async def test_import_statement_happy_path(tmp_path: Path):
    csv = (
        "Date,Description,Amount\n"
        "2026-01-03,Coffee Shop,-4.50\n"
        "2026-01-05,Paycheck,2000.00\n"
    )
    path = tmp_path / "bank.csv"
    path.write_text(csv)

    with patch(
        "agents.cfo.tools.find_import_by_hash", new=AsyncMock(return_value=None)
    ), patch(
        "agents.cfo.tools.record_import",
        new=AsyncMock(return_value=uuid4()),
    ), patch(
        "agents.cfo.tools.add_transactions", new=AsyncMock(return_value=2)
    ) as add:
        out = await _cfo_tool("import_statement")(
            {"path": str(path), "account": "chase_checking"}
        )

    assert out["imported"] is True
    assert out["account"] == "chase_checking"
    assert out["row_count"] == 2
    assert out["date_range"] == ["2026-01-03", "2026-01-05"]
    assert out["net_amount"] == Decimal("1995.50")
    # add_transactions got two normalized rows.
    rows = add.call_args.args[2]
    assert len(rows) == 2
    assert rows[0]["amount"] == Decimal("-4.50")


async def test_import_statement_skips_already_imported(tmp_path: Path):
    path = tmp_path / "dup.csv"
    path.write_text("Date,Description,Amount\n2026-01-03,X,-1.00\n")
    prior = {
        "account": "amex",
        "row_count": 9,
        "imported_at": "2026-05-01",
    }
    with patch(
        "agents.cfo.tools.find_import_by_hash",
        new=AsyncMock(return_value=prior),
    ), patch("agents.cfo.tools.add_transactions", new=AsyncMock()) as add:
        out = await _cfo_tool("import_statement")(
            {"path": str(path), "account": "amex"}
        )
    assert out["imported"] is False
    assert "already imported" in out["reason"]
    add.assert_not_called()  # nothing written on a re-import


async def test_import_statement_missing_file():
    out = await _cfo_tool("import_statement")(
        {"path": "/no/such/file.csv", "account": "amex"}
    )
    assert "error" in out and "no file" in out["error"]


async def test_import_statement_requires_account(tmp_path: Path):
    path = tmp_path / "x.csv"
    path.write_text("Date,Description,Amount\n2026-01-03,X,-1.00\n")
    out = await _cfo_tool("import_statement")({"path": str(path)})
    assert "error" in out


# ---------------------------------------------------------------------------
# cash_flow
# ---------------------------------------------------------------------------


async def test_cash_flow_by_month():
    summary = {"income_total": Decimal("100"), "net": Decimal("40")}
    with patch(
        "agents.cfo.tools.cash_flow", new=AsyncMock(return_value=summary)
    ) as cf:
        out = await _cfo_tool("cash_flow")({"year": 2026, "month": 2})
    assert out is summary
    assert cf.call_args.args == (date(2026, 2, 1), date(2026, 2, 28))


async def test_cash_flow_by_date_range():
    with patch(
        "agents.cfo.tools.cash_flow", new=AsyncMock(return_value={})
    ) as cf:
        await _cfo_tool("cash_flow")(
            {"start": "2026-01-01", "end": "2026-03-31"}
        )
    assert cf.call_args.args == (date(2026, 1, 1), date(2026, 3, 31))


async def test_cash_flow_defaults_to_current_month():
    from datetime import date

    from core.finance import month_range
    today = date.today()
    expected_start, expected_end = month_range(today.year, today.month)
    with patch(
        "agents.cfo.tools.cash_flow", new=AsyncMock(return_value={})
    ) as cf:
        await _cfo_tool("cash_flow")({})
    assert cf.call_args.args == (expected_start, expected_end)


async def test_cash_flow_rejects_partial_range():
    # If only one of start/end is given, that is a malformed call, not a
    # request to default; surface the error instead of silently defaulting.
    out = await _cfo_tool("cash_flow")({"start": "2026-01-01"})
    assert "error" in out


async def test_cash_flow_rejects_reversed_range():
    out = await _cfo_tool("cash_flow")(
        {"start": "2026-03-31", "end": "2026-01-01"}
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# categorize
# ---------------------------------------------------------------------------


async def test_categorize_by_match():
    with patch(
        "agents.cfo.tools.categorize_by_match", new=AsyncMock(return_value=7)
    ) as cm:
        out = await _cfo_tool("categorize")(
            {"match": "KROGER", "category": "groceries", "stream": "household"}
        )
    assert out["rows_changed"] == 7
    assert cm.call_args.args == ("KROGER", "groceries", "household", True)


async def test_categorize_by_id():
    tid = uuid4()
    with patch(
        "agents.cfo.tools.categorize_by_id", new=AsyncMock(return_value=True)
    ) as ci:
        out = await _cfo_tool("categorize")(
            {"transaction_id": str(tid), "category": "fuel"}
        )
    assert out["changed"] is True
    assert ci.call_args.args == (tid, "fuel", None)


async def test_categorize_needs_category_or_stream():
    out = await _cfo_tool("categorize")({"match": "KROGER"})
    assert "error" in out


async def test_categorize_rejects_bad_id():
    out = await _cfo_tool("categorize")(
        {"transaction_id": "not-a-uuid", "category": "fuel"}
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# recurring bills
# ---------------------------------------------------------------------------


async def test_add_recurring_bill():
    bid = uuid4()
    with patch(
        "agents.cfo.tools.add_recurring_bill",
        new=AsyncMock(return_value=bid),
    ) as ab:
        out = await _cfo_tool("add_recurring_bill")(
            {"name": "ACA premium", "amount": "$420.50", "cadence": "monthly"}
        )
    assert out["bill_id"] == str(bid)
    assert ab.call_args.kwargs["amount"] == Decimal("420.50")


async def test_add_recurring_bill_rejects_bad_cadence():
    out = await _cfo_tool("add_recurring_bill")(
        {"name": "X", "amount": "10", "cadence": "fortnightly"}
    )
    assert "error" in out


async def test_add_recurring_bill_rejects_bad_due_day():
    out = await _cfo_tool("add_recurring_bill")(
        {"name": "X", "amount": "10", "due_day": 45}
    )
    assert "error" in out


async def test_update_recurring_bill():
    bill = {"id": uuid4(), "name": "AWS"}
    with patch(
        "agents.cfo.tools.find_recurring_bill",
        new=AsyncMock(return_value=bill),
    ), patch(
        "agents.cfo.tools.update_recurring_bill",
        new=AsyncMock(return_value=True),
    ) as up:
        out = await _cfo_tool("update_recurring_bill")(
            {"bill_id": str(bill["id"]), "amount": "55.00", "status": "paused"}
        )
    assert out["changed"] is True
    assert set(out["updated_fields"]) == {"amount", "status"}
    assert up.call_args.kwargs["amount"] == Decimal("55.00")


async def test_update_recurring_bill_unknown_id():
    with patch(
        "agents.cfo.tools.find_recurring_bill",
        new=AsyncMock(return_value=None),
    ):
        out = await _cfo_tool("update_recurring_bill")(
            {"bill_id": "abcd1234", "amount": "1.00"}
        )
    assert "error" in out


# ---------------------------------------------------------------------------
# detect_anomalies and log_decision
# ---------------------------------------------------------------------------


async def test_detect_anomalies_passes_through():
    report = {"month": "2026-02", "category_swings": []}
    with patch(
        "agents.cfo.tools.detect_anomalies",
        new=AsyncMock(return_value=report),
    ) as da:
        out = await _cfo_tool("detect_anomalies")({"year": 2026, "month": 2})
    assert out is report
    assert da.call_args.args == (2026, 2)


async def test_detect_anomalies_requires_month():
    out = await _cfo_tool("detect_anomalies")({"year": 2026})
    assert "error" in out


async def test_log_decision_defaults_domain_to_money():
    did = uuid4()
    with patch(
        "agents.cfo.tools.log_decision", new=AsyncMock(return_value=did)
    ) as ld:
        out = await _cfo_tool("log_decision")(
            {"summary": "Raised vanity price to $260"}
        )
    assert out["decision_id"] == str(did)
    assert ld.call_args.kwargs["domain"] == "money"
    assert ld.call_args.kwargs["made_by"] == "owner"


async def test_log_decision_requires_summary():
    out = await _cfo_tool("log_decision")({"rationale": "because"})
    assert "error" in out
