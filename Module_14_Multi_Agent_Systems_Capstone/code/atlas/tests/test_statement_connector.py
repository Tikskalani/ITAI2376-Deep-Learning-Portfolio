"""Unit tests for the statement CSV connector.

Each test writes a small CSV into tmp_path, so no real statements or
external systems are touched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.connectors import (
    ConnectorError,
    StatementColumns,
    StatementConnector,
)


def _write(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


async def test_single_amount_column_autodetected(tmp_path: Path):
    csv = (
        "Date,Description,Amount\n"
        "2026-01-03,Coffee Shop,-4.50\n"
        "2026-01-05,Paycheck,2000.00\n"
    )
    path = _write(tmp_path, "bank.csv", csv)
    result = await StatementConnector().get(path)

    txns = result.data
    assert len(txns) == 2
    assert txns[0].posted == date(2026, 1, 3)
    assert txns[0].description == "Coffee Shop"
    assert txns[0].amount == Decimal("-4.50")
    assert txns[1].amount == Decimal("2000.00")
    assert result.stale is False


async def test_split_debit_credit_columns(tmp_path: Path):
    csv = (
        "Posting Date,Payee,Withdrawals,Deposits\n"
        "01/03/2026,Grocery,52.10,\n"
        "01/04/2026,Refund,,18.00\n"
    )
    path = _write(tmp_path, "split.csv", csv)
    txns = (await StatementConnector().get(path)).data

    assert txns[0].amount == Decimal("-52.10")  # withdrawal -> negative
    assert txns[1].amount == Decimal("18.00")   # deposit -> positive


async def test_flip_amount_sign_for_card_export(tmp_path: Path):
    # A card CSV that lists charges as positive numbers.
    csv = "Date,Description,Amount\n2026-02-01,Hardware Store,73.40\n"
    path = _write(tmp_path, "card.csv", csv)

    plain = (await StatementConnector().get(path)).data
    assert plain[0].amount == Decimal("73.40")

    flipped = (await StatementConnector(flip_amount_sign=True).get(path)).data
    assert flipped[0].amount == Decimal("-73.40")  # charge is money out


async def test_messy_amount_formatting(tmp_path: Path):
    csv = (
        "Date,Description,Amount\n"
        "2026-03-01,Big Deposit,\"$1,250.00\"\n"
        "2026-03-02,Parenthesized,(42.00)\n"
        "2026-03-03,Spaced, -7.25 \n"
    )
    path = _write(tmp_path, "messy.csv", csv)
    txns = (await StatementConnector().get(path)).data

    assert txns[0].amount == Decimal("1250.00")
    assert txns[1].amount == Decimal("-42.00")
    assert txns[2].amount == Decimal("-7.25")


async def test_varied_date_formats(tmp_path: Path):
    csv = (
        "Trans Date,Memo,Amount\n"
        "03/15/2026,A,-1.00\n"
        "15-Mar-2026,B,-2.00\n"
        "2026/03/16,C,-3.00\n"
    )
    path = _write(tmp_path, "dates.csv", csv)
    txns = (await StatementConnector().get(path)).data

    assert txns[0].posted == date(2026, 3, 15)
    assert txns[1].posted == date(2026, 3, 15)
    assert txns[2].posted == date(2026, 3, 16)


async def test_explicit_column_override(tmp_path: Path):
    # Headers auto-detection would not recognize.
    csv = (
        "When,What,HowMuch\n"
        "2026-04-01,Mystery Charge,-9.99\n"
    )
    path = _write(tmp_path, "weird.csv", csv)
    cols = StatementColumns(date="When", description="What", amount="HowMuch")
    txns = (await StatementConnector(columns=cols).get(path)).data

    assert txns[0].description == "Mystery Charge"
    assert txns[0].amount == Decimal("-9.99")


async def test_blank_trailing_line_skipped(tmp_path: Path):
    csv = "Date,Description,Amount\n2026-05-01,Only Row,-1.00\n\n"
    path = _write(tmp_path, "trailing.csv", csv)
    txns = (await StatementConnector().get(path)).data
    assert len(txns) == 1


async def test_raw_row_preserved(tmp_path: Path):
    csv = "Date,Description,Amount\n2026-01-03,Coffee,-4.50\n"
    path = _write(tmp_path, "raw.csv", csv)
    txns = (await StatementConnector().get(path)).data
    assert txns[0].raw == {
        "Date": "2026-01-03",
        "Description": "Coffee",
        "Amount": "-4.50",
    }


async def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConnectorError):
        await StatementConnector().get(str(tmp_path / "nope.csv"))


async def test_bad_date_raises(tmp_path: Path):
    csv = "Date,Description,Amount\nnot-a-date,Bad Row,-1.00\n"
    path = _write(tmp_path, "baddate.csv", csv)
    with pytest.raises(ConnectorError):
        await StatementConnector().get(path)


async def test_bad_amount_raises(tmp_path: Path):
    csv = "Date,Description,Amount\n2026-01-03,Bad Row,abc\n"
    path = _write(tmp_path, "badamt.csv", csv)
    with pytest.raises(ConnectorError):
        await StatementConnector().get(path)


async def test_no_amount_column_raises(tmp_path: Path):
    csv = "Date,Description\n2026-01-03,No Money Here\n"
    path = _write(tmp_path, "noamt.csv", csv)
    with pytest.raises(ConnectorError):
        await StatementConnector().get(path)


async def test_caching_returns_same_data(tmp_path: Path):
    csv = "Date,Description,Amount\n2026-01-03,Coffee,-4.50\n"
    path = _write(tmp_path, "cache.csv", csv)
    conn = StatementConnector()
    first = await conn.get(path)
    second = await conn.get(path)
    assert second.data is first.data  # served from cache, not re-read
