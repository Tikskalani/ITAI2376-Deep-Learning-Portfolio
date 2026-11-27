"""Money on Home (core/money.py, core/portfolio.py): a statement dropped, read-only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from agents.cfo.tools import build_tools as cfo_build_tools
from core import db, money, portfolio
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
HEADERS = {"X-Atlas-Client": "1", "Origin": "http://atlas"}

ROBINHOOD = """﻿"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"
"10/09/2026","10/09/2026","10/10/2026","","Robinhood Gold","GOLD","","","($5.00)"
"10/08/2026","10/08/2026","10/09/2026","NVDA","NVIDIA","Sell","5","$130.00","$650.00"
"10/07/2026","10/07/2026","10/08/2026","AAPL","Apple Cash Div: R/D 2026-10-01","CDIV","","","$3.20"
"10/03/2026","10/03/2026","10/06/2026","NVDA","NVIDIA","Buy","10","$120.00","($1,200.00)"
"10/02/2026","10/02/2026","10/03/2026","AAPL","Apple","Buy","4","$225.50","($902.00)"
"10/01/2026","10/01/2026","10/02/2026","NVDA","NVIDIA","Buy","10","$100.00","($1,000.00)"
"09/30/2026","09/30/2026","09/30/2026","","ACH Deposit","ACH","","","$3,500.00"
"09/29/2026","09/29/2026","09/29/2026","","Stock Lending","SLIP","","","$0.42"
"","","","","The data provided is for informational purposes only.","","","",""
"""

BANK = "Date,Description,Amount\n2026-10-05,H-E-B,-84.12\n2026-10-03,Payroll,2500.00\n2026-10-02,Shell,-52.30\n"


# ------------------------------------------------------------------ the parser and the math


def test_the_export_is_read_oldest_first_with_signs_and_kinds():
    trades = portfolio.parse_robinhood_csv(ROBINHOOD)
    assert [t.code for t in trades] == ["SLIP", "ACH", "BUY", "BUY", "BUY", "CDIV", "SELL", "GOLD"]
    assert [t.kind for t in trades] == ["interest", "deposit", "buy", "buy", "buy", "dividend", "sell", "fee"]
    first_buy = trades[2]
    assert (first_buy.instrument, first_buy.quantity, first_buy.price, first_buy.amount) == ("NVDA", Decimal("10"), Decimal("100.00"), Decimal("-1000.00"))
    assert first_buy.trade_date == date(2026, 10, 1) and first_buy.settle_date == date(2026, 10, 2)
    assert trades[0].amount == Decimal("0.42") and trades[-1].amount == Decimal("-5.00") and trades[-1].instrument is None
    assert portfolio.looks_like_robinhood(["Activity Date", "Trans Code", "Amount"]) and not portfolio.looks_like_robinhood(["Date", "Amount"])
    with pytest.raises(ValueError):
        portfolio.parse_robinhood_csv(BANK)
    with pytest.raises(ValueError):
        portfolio.parse_robinhood_csv(ROBINHOOD.replace('"$650.00"', '"six fifty"'))


def test_positions_use_average_cost_and_realize_gains_on_each_sale():
    out = portfolio.positions(portfolio.parse_robinhood_csv(ROBINHOOD))
    by = {p["instrument"]: p for p in out["positions"]}
    nvda = by["NVDA"]
    # 10 @ 100 and 10 @ 120: average 110. Sold 5 @ 130: 650 - 550 = 100 realized; 15 left at 1650.
    assert nvda["shares"] == Decimal("15") and nvda["avg_cost"] == Decimal("110.0000")
    assert nvda["cost_basis"] == Decimal("1650.00") and nvda["realized"] == Decimal("100.00")
    aapl = by["AAPL"]
    assert aapl["shares"] == Decimal("4") and aapl["cost_basis"] == Decimal("902.00") and aapl["dividends"] == Decimal("3.20")
    assert out["totals"] == {"deposits": Decimal("3500.00"), "withdrawals": Decimal("0.00"), "dividends": Decimal("3.20"),
                             "fees": Decimal("5.00"), "interest": Decimal("0.42"), "realized": Decimal("100.00"),
                             "invested": Decimal("3102.00")}
    assert out["cost_basis"] == Decimal("2552.00") and out["last_activity"] == date(2026, 10, 9) and out["closed"] == []
    # Selling everything closes the position; selling more than held realizes against what was held.
    extra = ROBINHOOD + '"10/10/2026","10/10/2026","10/11/2026","AAPL","Apple","Sell","4","$230.00","$920.00"\n'
    out2 = portfolio.positions(portfolio.parse_robinhood_csv(extra))
    assert [p["instrument"] for p in out2["positions"]] == ["NVDA"] and out2["closed"][0]["realized"] == Decimal("18.00")


# ------------------------------------------------------------------ filing


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch, tmp_path):
    monkeypatch.setattr(money, "STATEMENTS_DIR", tmp_path / "statements")
    async with livedb.shadow(monkeypatch, "statement_imports", "transactions", "trades"):
        yield tmp_path


@aio
async def test_an_export_is_filed_once_and_the_cash_rows_reach_cash_flow(world):
    path = world / "robinhood.csv"
    path.write_text(ROBINHOOD, encoding="utf-8")
    out = await portfolio.import_file(path)
    assert out["imported"] and out["row_count"] == 8 and out["cash_rows"] == 4 and out["date_range"] == ["2026-09-29", "2026-10-09"]
    assert await db.fetchval("select count(*) from trades where account = 'robinhood'") == 8
    cash = await db.fetch("select description, amount, stream from transactions where account = 'robinhood' order by posted")
    assert [(r["description"], r["amount"]) for r in cash] == [("SLIP  Stock Lending", Decimal("0.42")), ("ACH  ACH Deposit", Decimal("3500.00")),
                                                              ("CDIV AAPL Apple Cash Div: R/D 2026-10-01", Decimal("3.20")), ("GOLD  Robinhood Gold", Decimal("-5.00"))]
    assert {r["stream"] for r in cash} == {"investing"}
    again = await portfolio.import_file(path)
    assert again["imported"] is False and "already imported" in again["reason"]
    held = await portfolio.holdings()
    assert [p["instrument"] for p in held["positions"]] == ["AAPL", "NVDA"] and held["trades"] == 8 and held["last_import"]["rows"] == 8

    # The CFO's tools see it.
    tools = {t.name: t.handler for t in cfo_build_tools()}
    assert (await tools["portfolio"]({}))["totals"]["realized"] == Decimal("100.00")
    assert "No Robinhood export" in (await tools["portfolio"]({"account": "other"}))["note"]
    path2 = world / "robinhood-2.csv"
    path2.write_text(ROBINHOOD.replace("09/29/2026", "09/28/2026"), encoding="utf-8")
    via_cfo = await tools["import_statement"]({"path": str(path2), "account": "robinhood"})
    assert via_cfo["imported"] and via_cfo["row_count"] == 8 and "import_id" not in via_cfo


@aio
async def test_the_home_card_files_either_kind_and_shows_where_he_stands(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas", headers=HEADERS) as c:
        r = await c.get("/api/money")
        assert r.status_code == 200 and r.json()["empty"] and r.json()["portfolio"]["positions"] == []
        r = await c.post("/api/money/import", files={"file": ("robinhood (1).csv", ROBINHOOD.encode(), "text/csv")})
        assert r.status_code == 200 and r.json()["kind"] == "robinhood" and r.json()["imported"] and r.json()["row_count"] == 8
        r = await c.post("/api/money/import", files={"file": ("chase_checking.csv", BANK.encode(), "text/csv")})
        assert r.status_code == 200 and r.json()["kind"] == "statement" and r.json()["account"] == "chase_checking" and r.json()["row_count"] == 3
        r = await c.post("/api/money/import", files={"file": ("card.csv", BANK.encode(), "text/csv")}, data={"account": "amex"})
        assert r.status_code == 200 and r.json()["imported"] is False               # same bytes, already filed
        assert (await c.post("/api/money/import", files={"file": ("x.csv", b"", "text/csv")})).status_code == 422
        assert (await c.post("/api/money/import", files={"file": ("s.pdf", b"%PDF-1.4\x00\xff", "application/pdf")})).status_code == 422
        assert (await c.post("/api/money/import", files={"file": ("odd.csv", b"a,b\n1,2\n", "text/csv")})).status_code == 422

        r = await c.get("/api/money", params={"days": 30})
        j = r.json()
        assert not j["empty"] and sorted(a["account"] for a in j["accounts"]) == ["chase_checking", "robinhood"]
        assert j["portfolio"]["positions"][1]["instrument"] == "NVDA" and float(j["portfolio"]["totals"]["invested"]) == 3102.0
        assert len(j["recent"]) >= 3 and j["cash_flow"]["transaction_count"] >= 3
    saved = sorted(p.name for p in (world / "statements").iterdir())
    assert len(saved) == 3 and saved[0].endswith("-robinhood-1-.csv") or any("robinhood" in n for n in saved)
