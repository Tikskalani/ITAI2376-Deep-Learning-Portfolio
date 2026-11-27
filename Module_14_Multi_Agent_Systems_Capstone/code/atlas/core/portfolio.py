"""Atlas -- Robinhood, read-only: the export he downloads, never the account.

Robinhood has no personal API, and its new agentic trading product links
an agent to a brokerage account to place trades, which Atlas will never
do. What Atlas reads is the transaction history he exports (Account,
Reports and statements, a date range, CSV): one row per activity with an
Activity Date, an Instrument, a Trans Code (Buy, Sell, CDIV for a
dividend, ACH for a deposit or withdrawal, AFEE for a fee, GOLD for the
subscription, SLIP for stock lending, INT for interest) and a signed
Amount written the way accountants write it, "($1,234.56)" for money out.

import_file() keeps every row in trades, puts the cash movements into
transactions so cash flow sees them, and refuses the same file twice (the
hash guard every statement import uses). positions() recomputes holdings
from the record: shares held and average cost per instrument, realized
gains on each sale against that average, dividends, fees, deposits.
There are no quotes here; the numbers are cost basis, and the card says
so.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from core import db, finance

log = logging.getLogger(__name__)

ACCOUNT = "robinhood"
_REQUIRED = {"activity date", "trans code", "amount"}
_KINDS = {
    "BUY": "buy", "SELL": "sell", "CDIV": "dividend", "DIV": "dividend", "ACH": "cash", "RTP": "cash",
    "AFEE": "fee", "DFEE": "fee", "GOLD": "fee", "MINT": "fee", "SLIP": "interest", "INT": "interest",
    "MRGN": "fee", "SPL": "other", "SPR": "other", "CONV": "other", "REC": "other",
}
_ZERO = Decimal("0")


@dataclass
class Trade:
    trade_date: date
    settle_date: date | None
    instrument: str | None
    description: str
    code: str
    kind: str
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal
    raw: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------ the file


def looks_like_robinhood(headers: list[str]) -> bool:
    low = {h.strip().lower() for h in headers if h}
    return _REQUIRED <= low


def _money(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.startswith("-"):
        neg, s = True, s[1:]
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {raw!r}") from exc
    return -value if neg else value


def _date(raw: str | None) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {raw!r}")


def _kind(code: str, amount: Decimal) -> str:
    kind = _KINDS.get(code.upper(), "other")
    if kind == "cash":
        return "deposit" if amount >= _ZERO else "withdrawal"
    return kind


def parse_robinhood_csv(text: str) -> list[Trade]:
    """Every activity row, oldest first. A row with a date and nothing else
    (Robinhood's footer) is skipped; a row that cannot be read raises."""
    reader = csv.DictReader(text.lstrip("﻿").splitlines())
    headers = [h for h in (reader.fieldnames or []) if h]
    if not looks_like_robinhood(headers):
        raise ValueError(f"not a Robinhood export: headers {headers!r}")
    col = {h.strip().lower(): h for h in headers}

    def get(row: dict[str, Any], name: str) -> str:
        return (row.get(col.get(name, ""), "") or "").strip()

    out: list[Trade] = []
    for n, row in enumerate(reader, start=2):
        values = [(v or "").strip() for v in row.values() if v is not None]
        if not any(values):
            continue
        code = get(row, "trans code")
        amount_raw = get(row, "amount")
        if not code and not amount_raw:
            continue  # the "data provided is for informational purposes" footer
        try:
            when = _date(get(row, "activity date"))
            if when is None:
                raise ValueError("no activity date")
            amount = _money(amount_raw)
            if amount is None:
                raise ValueError("no amount")
            quantity = _money(get(row, "quantity"))
            price = _money(get(row, "price"))
        except ValueError as exc:
            raise ValueError(f"row {n}: {exc}") from exc
        out.append(Trade(
            trade_date=when, settle_date=_date(get(row, "settle date")) if "settle date" in col else None,
            instrument=get(row, "instrument") or None, description=get(row, "description"), code=code.upper(),
            kind=_kind(code, amount), quantity=quantity, price=price, amount=amount,
            raw={k: (v or "") for k, v in row.items() if k},
        ))
    out.sort(key=lambda t: t.trade_date)
    return out


# ------------------------------------------------------------------ positions


def positions(trades: Sequence[Trade | dict[str, Any]]) -> dict[str, Any]:
    """Holdings from the record, average cost. Realized gains are each sale's
    proceeds against the average cost of the shares sold at that moment."""
    lots: dict[str, dict[str, Decimal]] = {}
    totals = {"deposits": _ZERO, "withdrawals": _ZERO, "dividends": _ZERO, "fees": _ZERO,
              "interest": _ZERO, "realized": _ZERO, "invested": _ZERO}
    last: date | None = None
    for t in sorted(trades, key=lambda x: _f(x, "trade_date")):
        kind, amount = _f(t, "kind"), Decimal(_f(t, "amount"))
        inst = _f(t, "instrument")
        last = _f(t, "trade_date") if last is None or _f(t, "trade_date") > last else last
        if kind == "deposit":
            totals["deposits"] += amount
        elif kind == "withdrawal":
            totals["withdrawals"] += -amount
        elif kind == "dividend":
            totals["dividends"] += amount
            if inst:
                lots.setdefault(inst, _lot())["dividends"] += amount
        elif kind == "fee":
            totals["fees"] += -amount
        elif kind == "interest":
            totals["interest"] += amount
        elif kind in ("buy", "sell") and inst:
            lot = lots.setdefault(inst, _lot())
            qty = Decimal(_f(t, "quantity") or 0)
            if kind == "buy":
                cost = -amount  # money out
                lot["shares"] += qty
                lot["cost"] += cost
                totals["invested"] += cost
            else:
                held = lot["shares"]
                avg = (lot["cost"] / held) if held else _ZERO
                sold = min(qty, held) if held else qty
                basis = avg * sold
                gain = amount - basis
                lot["realized"] += gain
                totals["realized"] += gain
                lot["shares"] -= sold
                lot["cost"] -= basis
                if lot["shares"] <= Decimal("0.000001"):
                    lot["shares"], lot["cost"] = _ZERO, _ZERO
    rows: list[dict[str, Any]] = []
    for inst, lot in sorted(lots.items()):
        shares = lot["shares"]
        rows.append({
            "instrument": inst, "shares": _q(shares, 6), "cost_basis": _q(lot["cost"], 2),
            "avg_cost": _q(lot["cost"] / shares, 4) if shares else None,
            "realized": _q(lot["realized"], 2), "dividends": _q(lot["dividends"], 2),
            "open": shares > _ZERO,
        })
    open_rows = [r for r in rows if r["open"]]
    return {
        "positions": open_rows, "closed": [r for r in rows if not r["open"]],
        "totals": {k: _q(v, 2) for k, v in totals.items()},
        "cost_basis": _q(sum((Decimal(r["cost_basis"]) for r in open_rows), _ZERO), 2),
        "last_activity": last,
        "note": "Cost basis from the export; no quotes, so no market value here.",
    }


def _lot() -> dict[str, Decimal]:
    return {"shares": _ZERO, "cost": _ZERO, "realized": _ZERO, "dividends": _ZERO}


def _f(t: Any, name: str) -> Any:
    return t[name] if isinstance(t, dict) else getattr(t, name)


def _q(v: Decimal, places: int) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-places))


# ------------------------------------------------------------------ the import


async def import_file(path: Path | str, *, account: str = ACCOUNT) -> dict[str, Any]:
    """File the export once: trades, the cash rows into transactions, the hash."""
    p = Path(path)
    text = await asyncio.to_thread(p.read_text, encoding="utf-8-sig")
    content_hash = await asyncio.to_thread(finance.hash_file, str(p))
    existing = await finance.find_import_by_hash(content_hash)
    if existing is not None:
        return {"imported": False, "reason": "this exact file was already imported", "account": existing["account"],
                "imported_at": existing["imported_at"], "row_count": existing["row_count"]}
    trades = parse_robinhood_csv(text)
    if not trades:
        return {"imported": False, "reason": "no activity rows in the file"}
    import_id = await finance.record_import(account, str(p), content_hash, len(trades))
    await db.execute(
        """
        insert into trades (import_id, account, trade_date, settle_date, instrument, description, code, kind,
                            quantity, price, amount, metadata)
        select $1, $2, d, s, i, de, c, k, q, pr, a, m
        from unnest($3::date[], $4::date[], $5::text[], $6::text[], $7::text[], $8::text[],
                    $9::numeric[], $10::numeric[], $11::numeric[], $12::jsonb[]) as t(d, s, i, de, c, k, q, pr, a, m)
        """,
        import_id, account,
        [t.trade_date for t in trades], [t.settle_date for t in trades], [t.instrument for t in trades],
        [t.description for t in trades], [t.code for t in trades], [t.kind for t in trades],
        [t.quantity for t in trades], [t.price for t in trades], [t.amount for t in trades],
        [{"raw": t.raw} for t in trades],
    )
    cash = [t for t in trades if t.kind in ("deposit", "withdrawal", "dividend", "fee", "interest")]
    cash_rows = await finance.add_transactions(import_id, account, [
        {"posted": t.trade_date, "description": f"{t.code} {t.instrument or ''} {t.description}".strip(),
         "amount": t.amount, "metadata": {"raw": t.raw, "kind": t.kind}}
        for t in cash
    ])
    if cash_rows:
        await db.execute("update transactions set stream = 'investing' where import_id = $1", import_id)
    summary = positions(trades)
    dates = [t.trade_date for t in trades]
    return {"imported": True, "account": account, "row_count": len(trades), "cash_rows": cash_rows,
            "date_range": [min(dates).isoformat(), max(dates).isoformat()], "import_id": import_id,
            "positions": summary["positions"], "totals": summary["totals"]}


async def holdings(account: str = ACCOUNT) -> dict[str, Any]:
    """Positions from everything imported for the account."""
    rows = await db.fetch(
        "select trade_date, instrument, kind, quantity, amount from trades where account = $1 order by trade_date, created_at",
        account,
    )
    out = positions([dict(r) for r in rows])
    last = await db.fetchrow(
        "select imported_at, row_count from statement_imports where account = $1 order by imported_at desc limit 1", account)
    out["account"] = account
    out["last_import"] = {"at": last["imported_at"], "rows": last["row_count"]} if last else None
    out["trades"] = len(rows)
    return out


def trade_import_id(result: dict[str, Any]) -> UUID | None:
    return result.get("import_id")


__all__ = ["ACCOUNT", "Trade", "looks_like_robinhood", "parse_robinhood_csv", "positions", "import_file", "holdings"]
