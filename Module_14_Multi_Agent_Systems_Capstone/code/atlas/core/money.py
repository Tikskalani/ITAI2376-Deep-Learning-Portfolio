"""Atlas -- Money on Home: drop a statement, see where you stand.

One place for the two things he exports by hand: a bank or card statement
(the CFO's statement connector) and Robinhood's transaction history
(core/portfolio.py). import_upload() files whichever arrived, under
data/statements (ignored by git, like everything under data/), and
overview() is what the Home card shows: cash in and out over the window,
the biggest categories, the accounts and when each was last imported,
and the portfolio at cost basis. Read-only all the way: Atlas never holds
a bank login or the brokerage account.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import db, finance, portfolio
from core.config import REPO_ROOT
from core.connectors.base import ConnectorError
from core.connectors.statement import StatementConnector

log = logging.getLogger(__name__)

STATEMENTS_DIR = REPO_ROOT / "data" / "statements"
MAX_BYTES = 20 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


async def import_statement(path: Path | str, *, account: str, flip_amount_sign: bool = False) -> dict[str, Any]:
    """A bank or card CSV into transactions, once (the hash guard)."""
    p = str(path)
    account = (account or "").strip()
    if not account:
        return {"error": "account is required (a label like chase_checking)"}
    try:
        content_hash = finance.hash_file(p)
    except FileNotFoundError:
        return {"error": f"no file found at {p}"}
    except OSError as exc:
        return {"error": f"could not read {p}: {exc}"}
    existing = await finance.find_import_by_hash(content_hash)
    if existing is not None:
        return {"imported": False, "reason": "this exact file was already imported", "account": existing["account"],
                "imported_at": existing["imported_at"], "row_count": existing["row_count"]}
    connector = StatementConnector(flip_amount_sign=flip_amount_sign)
    try:
        result = await connector.get(p)
    except ConnectorError as exc:
        return {"error": f"could not parse statement: {exc}"}
    transactions = result.data
    if not transactions:
        return {"imported": False, "reason": "no transactions found in file"}
    import_id = await finance.record_import(account, p, content_hash, len(transactions))
    count = await finance.add_transactions(import_id, account, [
        {"posted": t.posted, "description": t.description, "amount": t.amount, "metadata": {"raw": t.raw}}
        for t in transactions
    ])
    dates = [t.posted for t in transactions]
    net = sum((t.amount for t in transactions), Decimal("0"))
    return {"imported": True, "account": account, "row_count": count,
            "date_range": [min(dates).isoformat(), max(dates).isoformat()], "net_amount": net,
            "note": "transactions are uncategorized; categorize them next"}


def _headers(text: str) -> list[str]:
    reader = csv.reader(text.lstrip("﻿").splitlines()[:1])
    return next(reader, [])


def kind_of(text: str) -> str:
    return "robinhood" if portfolio.looks_like_robinhood(_headers(text)) else "statement"


def _peek(path: str, chars: int = 4000) -> str:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read(chars)
    except OSError:
        return ""


async def kind_of_file(path: Path | str) -> str:
    """What a file on disk is, from its header row, off the event loop."""
    return kind_of(await asyncio.to_thread(_peek, str(path)))


async def import_upload(filename: str, data: bytes, *, account: str | None = None,
                        flip_amount_sign: bool = False) -> dict[str, Any]:
    """A file from the Home card: saved under data/statements, then filed by kind."""
    if not data:
        return {"error": "the file is empty"}
    if len(data) > MAX_BYTES:
        return {"error": "the file is larger than 20 MB"}
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"error": "not a text CSV; Robinhood's monthly statement is a PDF, export the transaction history as CSV instead"}
    safe = _SAFE.sub("-", Path(filename or "statement.csv").name).strip("-") or "statement.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATEMENTS_DIR / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{safe}"
    path.write_bytes(data)
    kind = kind_of(text)
    if kind == "robinhood":
        try:
            out = await portfolio.import_file(path, account=(account or "").strip() or portfolio.ACCOUNT)
        except ValueError as exc:
            return {"error": f"could not read the Robinhood export: {exc}", "kind": kind}
        return {**out, "kind": kind, "path": str(path)}
    label = (account or "").strip() or _SAFE.sub("_", Path(safe).stem).strip("_").lower() or "statement"
    out = await import_statement(path, account=label, flip_amount_sign=flip_amount_sign)
    return {**out, "kind": kind, "path": str(path)}


async def overview(days: int = 30) -> dict[str, Any]:
    """What the Home card shows."""
    days = max(7, min(int(days), 365))
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)
    flow = await finance.cash_flow(start, end)
    accounts = await db.fetch(
        """
        select account, max(imported_at) as last_import, sum(row_count) as rows, count(*) as files
        from statement_imports group by account order by account
        """
    )
    recent = await db.fetch(
        "select posted, description, amount, account, category from transactions "
        "where posted between $1 and $2 order by posted desc, created_at desc limit 8",
        start, end,
    )
    port = await portfolio.holdings()
    return {
        "days": days, "start": start, "end": end,
        "cash_flow": {k: flow[k] for k in ("income_total", "expense_total", "net", "transaction_count", "by_category")},
        "accounts": [dict(r) for r in accounts],
        "recent": [dict(r) for r in recent],
        "portfolio": port,
        "empty": not accounts,
    }


__all__ = ["STATEMENTS_DIR", "MAX_BYTES", "import_statement", "import_upload", "kind_of", "overview"]
