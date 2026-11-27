"""Atlas -- CFO agent tools.

The CFO's toolset covers four areas:
  * statement import   -- import_statement
  * cash flow + detail -- cash_flow, list_transactions
  * categorization     -- categorize
  * recurring bills    -- add_recurring_bill, list_recurring_bills,
                          update_recurring_bill
  * anomalies          -- detect_anomalies
  * the decision log   -- log_decision
plus the shared memory tools from core.runtime.common_tools.

build_tools() returns the ToolSpec list the runtime hands to the model.
The dispatcher calls it through the CFO's registration (see agents/cfo
register_cfo).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from core.connectors import ConnectorError, StatementConnector
from core.decisions import log_decision
from core.finance import (
    add_recurring_bill,
    add_transactions,
    cash_flow,
    categorize_by_id,
    categorize_by_match,
    detect_anomalies,
    find_import_by_hash,
    find_recurring_bill,
    hash_file,
    list_recurring_bills,
    list_transactions,
    month_range,
    record_import,
    update_recurring_bill,
)
from core.runtime.agent import ToolSpec
from core.runtime.common_tools import (
    recall_facts_tool,
    recall_lessons_tool,
    remember_fact_tool,
)

_CADENCES = ("weekly", "monthly", "quarterly", "annual")
_BILL_STATUSES = ("active", "paused", "ended")


def _parse_iso_date(raw: Any) -> tuple[date | None, str | None]:
    """Parse an optional ISO date argument. Returns (date, error_message)."""
    if not raw:
        return None, None
    try:
        return date.fromisoformat(str(raw).strip()), None
    except ValueError:
        return None, f"date must be ISO format YYYY-MM-DD, got {raw!r}"


def _parse_amount(raw: Any) -> tuple[Decimal | None, str | None]:
    """Parse a money argument into a Decimal. Returns (amount, error)."""
    if raw is None or str(raw).strip() == "":
        return None, "amount is required"
    text = str(raw).strip().replace("$", "").replace(",", "")
    try:
        return Decimal(text), None
    except InvalidOperation:
        return None, f"amount is not a number: {raw!r}"


def _clamp_limit(raw: Any, default: int = 50, ceiling: int = 500) -> int:
    """Coerce a limit argument into a sane range."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))


# ---------------------------------------------------------------------------
# Statement import
# ---------------------------------------------------------------------------


async def _import_statement(args: dict[str, Any]) -> dict[str, Any]:
    """Import a bank or card statement CSV into the finance store."""
    path = (args.get("path") or "").strip()
    account = (args.get("account") or "").strip()
    if not path:
        return {"error": "path is required"}
    if not account:
        return {"error": "account is required (a label like chase_checking)"}
    flip = bool(args.get("flip_amount_sign"))

    try:
        content_hash = hash_file(path)
    except FileNotFoundError:
        return {"error": f"no file found at {path}"}
    except OSError as exc:
        return {"error": f"could not read {path}: {exc}"}

    existing = await find_import_by_hash(content_hash)
    if existing is not None:
        return {
            "imported": False,
            "reason": "this exact file was already imported",
            "account": existing["account"],
            "imported_at": existing["imported_at"],
            "row_count": existing["row_count"],
        }

    # Robinhood's transaction export has its own shape and its own record
    # (trades, positions); the cash movements still land in transactions.
    from core import money, portfolio

    if await money.kind_of_file(path) == "robinhood":
        try:
            out = await portfolio.import_file(path, account=account)
        except ValueError as exc:
            return {"error": f"could not read the Robinhood export: {exc}"}
        return {k: v for k, v in out.items() if k != "import_id"}

    connector = StatementConnector(flip_amount_sign=flip)
    try:
        result = await connector.get(path)
    except ConnectorError as exc:
        return {"error": f"could not parse statement: {exc}"}

    transactions = result.data
    if not transactions:
        return {"imported": False, "reason": "no transactions found in file"}

    import_id = await record_import(
        account, path, content_hash, len(transactions)
    )
    rows = [
        {
            "posted": t.posted,
            "description": t.description,
            "amount": t.amount,
            "metadata": {"raw": t.raw},
        }
        for t in transactions
    ]
    count = await add_transactions(import_id, account, rows)
    dates = [t.posted for t in transactions]
    net = sum((t.amount for t in transactions), Decimal("0"))
    return {
        "imported": True,
        "account": account,
        "row_count": count,
        "date_range": [min(dates).isoformat(), max(dates).isoformat()],
        "net_amount": net,
        "note": "transactions are uncategorized; categorize them next",
    }


# ---------------------------------------------------------------------------
# Cash flow and detail
# ---------------------------------------------------------------------------


async def _cash_flow(args: dict[str, Any]) -> dict[str, Any]:
    """Aggregate cash flow for a month or an explicit date range.

    With no args at all, defaults to the current calendar month. That
    matches the common "what does this month look like" call and lets a
    new install return zeros instead of an error before any data is in.
    """
    year = args.get("year")
    month = args.get("month")
    start_arg = args.get("start")
    end_arg = args.get("end")

    if year and month:
        try:
            start, end = month_range(int(year), int(month))
        except (ValueError, TypeError) as exc:
            return {"error": f"bad year/month: {exc}"}
    elif start_arg or end_arg:
        start_d, err = _parse_iso_date(start_arg)
        if err:
            return {"error": f"start {err}"}
        end_d, err = _parse_iso_date(end_arg)
        if err:
            return {"error": f"end {err}"}
        if start_d is None or end_d is None:
            return {"error": "give both start and end, or a year and month"}
        if end_d < start_d:
            return {"error": "end date is before start date"}
        start, end = start_d, end_d
    else:
        today = date.today()
        start, end = month_range(today.year, today.month)

    return await cash_flow(start, end)


async def _list_transactions(args: dict[str, Any]) -> dict[str, Any]:
    """List individual transactions for a date range."""
    start, err = _parse_iso_date(args.get("start"))
    if err:
        return {"error": f"start {err}"}
    end, err = _parse_iso_date(args.get("end"))
    if err:
        return {"error": f"end {err}"}
    if start is None or end is None:
        return {"error": "start and end dates are both required"}
    if end < start:
        return {"error": "end date is before start date"}
    account = (args.get("account") or "").strip() or None
    stream = (args.get("stream") or "").strip() or None
    limit = _clamp_limit(args.get("limit"))
    rows = await list_transactions(start, end, account, stream, limit)
    return {
        "count": len(rows),
        "limit": limit,
        "transactions": [
            {
                "id": str(r["id"]),
                "posted": r["posted"].isoformat(),
                "description": r["description"],
                "amount": r["amount"],
                "category": r["category"],
                "stream": r["stream"],
                "account": r["account"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


async def _categorize(args: dict[str, Any]) -> dict[str, Any]:
    """Assign a category and/or stream, by transaction id or by match."""
    category = (args.get("category") or "").strip() or None
    stream = (args.get("stream") or "").strip() or None
    if category is None and stream is None:
        return {"error": "give a category, a stream, or both"}

    txn_id = (args.get("transaction_id") or "").strip()
    match = (args.get("match") or "").strip()

    if txn_id:
        try:
            uid = UUID(txn_id)
        except ValueError:
            return {"error": f"transaction_id is not a valid id: {txn_id!r}"}
        changed = await categorize_by_id(uid, category, stream)
        return {
            "transaction_id": txn_id,
            "changed": changed,
            "category": category,
            "stream": stream,
        }

    if match:
        only_uncategorized = bool(args.get("only_uncategorized", True))
        count = await categorize_by_match(
            match, category, stream, only_uncategorized
        )
        return {
            "match": match,
            "rows_changed": count,
            "category": category,
            "stream": stream,
            "only_uncategorized": only_uncategorized,
        }

    return {"error": "give either a transaction_id or a match string"}


# ---------------------------------------------------------------------------
# Recurring bills
# ---------------------------------------------------------------------------


def _bill_view(bill: dict[str, Any]) -> dict[str, Any]:
    """Trim a bill row to the fields worth showing the model."""
    return {
        "id": str(bill["id"]),
        "name": bill["name"],
        "amount": bill["amount"],
        "cadence": bill["cadence"],
        "due_day": bill["due_day"],
        "account": bill["account"],
        "stream": bill["stream"],
        "status": bill["status"],
        "last_seen": (
            bill["last_seen"].isoformat() if bill["last_seen"] else None
        ),
        "notes": bill["notes"],
    }


async def _add_recurring_bill(args: dict[str, Any]) -> dict[str, Any]:
    """Add a recurring bill for the CFO to watch."""
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    amount, err = _parse_amount(args.get("amount"))
    if err or amount is None:
        return {"error": err or "amount is required"}

    cadence = (args.get("cadence") or "monthly").strip().lower()
    if cadence not in _CADENCES:
        return {"error": f"cadence must be one of {_CADENCES}, got {cadence!r}"}

    due_day_raw = args.get("due_day")
    due_day: int | None = None
    if due_day_raw not in (None, ""):
        try:
            due_day = int(due_day_raw)
        except (TypeError, ValueError):
            return {"error": f"due_day must be a number 1-31, got {due_day_raw!r}"}
        if not 1 <= due_day <= 31:
            return {"error": f"due_day must be 1-31, got {due_day}"}

    bill_id = await add_recurring_bill(
        name=name,
        amount=abs(amount),
        cadence=cadence,
        due_day=due_day,
        account=(args.get("account") or "").strip() or None,
        stream=(args.get("stream") or "").strip() or None,
        notes=(args.get("notes") or "").strip() or None,
    )
    return {"bill_id": str(bill_id), "added": name}


async def _list_recurring_bills(args: dict[str, Any]) -> dict[str, Any]:
    """List the recurring bills, active ones by default."""
    status = (args.get("status") or "active").strip().lower()
    bills = await list_recurring_bills(
        status=None if status == "all" else status
    )
    return {
        "status_filter": status,
        "count": len(bills),
        "bills": [_bill_view(b) for b in bills],
    }


async def _update_recurring_bill(args: dict[str, Any]) -> dict[str, Any]:
    """Update a recurring bill's amount, cadence, status, and so on."""
    raw = (args.get("bill_id") or "").strip()
    if not raw:
        return {"error": "bill_id is required"}
    bill = await find_recurring_bill(raw)
    if bill is None:
        return {"error": f"no bill matches {raw!r}"}

    fields: dict[str, Any] = {}
    if args.get("amount") not in (None, ""):
        amount, err = _parse_amount(args.get("amount"))
        if err or amount is None:
            return {"error": err or "amount is required"}
        fields["amount"] = abs(amount)
    if args.get("cadence"):
        cadence = str(args["cadence"]).strip().lower()
        if cadence not in _CADENCES:
            return {"error": f"cadence must be one of {_CADENCES}"}
        fields["cadence"] = cadence
    if args.get("status"):
        status = str(args["status"]).strip().lower()
        if status not in _BILL_STATUSES:
            return {"error": f"status must be one of {_BILL_STATUSES}"}
        fields["status"] = status
    if args.get("notes") is not None and str(args.get("notes")).strip():
        fields["notes"] = str(args["notes"]).strip()
    if args.get("last_seen"):
        last_seen, err = _parse_iso_date(args.get("last_seen"))
        if err:
            return {"error": f"last_seen {err}"}
        fields["last_seen"] = last_seen

    if not fields:
        return {"error": "nothing to update; pass at least one field"}
    changed = await update_recurring_bill(bill["id"], **fields)
    return {
        "bill_id": str(bill["id"]),
        "name": bill["name"],
        "changed": changed,
        "updated_fields": sorted(fields),
    }


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


async def _detect_anomalies(args: dict[str, Any]) -> dict[str, Any]:
    """Flag spending anomalies for a given month."""
    year = args.get("year")
    month = args.get("month")
    if not year or not month:
        return {"error": "year and month are both required"}
    try:
        return await detect_anomalies(int(year), int(month))
    except (ValueError, TypeError) as exc:
        return {"error": f"bad year/month: {exc}"}


# ---------------------------------------------------------------------------
# Decision journal
# ---------------------------------------------------------------------------


async def _log_decision(args: dict[str, Any]) -> dict[str, Any]:
    """Record a financial decision with its rationale and expected outcome."""
    summary = (args.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required and cannot be empty"}
    review_at, err = _parse_iso_date(args.get("review_at"))
    if err:
        return {"error": f"review_{err}"}
    decision_id = await log_decision(
        summary=summary,
        rationale=(args.get("rationale") or "").strip() or None,
        expected_outcome=(args.get("expected_outcome") or "").strip() or None,
        made_by="owner",
        domain=(args.get("domain") or "money").strip() or "money",
        review_at=review_at,
    )
    return {"decision_id": str(decision_id), "logged": summary}


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


async def _portfolio(args: dict[str, Any]) -> dict[str, Any]:
    """Positions at cost from the Robinhood exports he has filed."""
    from core import portfolio

    account = (args.get("account") or "").strip() or portfolio.ACCOUNT
    out = await portfolio.holdings(account)
    if not out["trades"]:
        return {"account": account, "positions": [], "note": "No Robinhood export filed yet. He exports the "
                "transaction history as CSV (Account, Reports and statements) and drops it on the Money card."}
    return out


_PORTFOLIO = ToolSpec(
    name="portfolio",
    description=(
        "Trilok's Robinhood positions at cost, from the transaction exports he "
        "has filed: shares held and average cost per instrument, realized gains "
        "on sales, dividends, fees, deposits. No quotes, so no market value; say "
        "so when he asks what it is worth. Read-only: Atlas never trades."
    ),
    input_schema={"type": "object", "properties": {"account": {"type": "string", "description": "Account label; default robinhood."}}},
    handler=_portfolio,
)

_IMPORT_STATEMENT = ToolSpec(
    name="import_statement",
    description=(
        "Import a bank or credit-card statement CSV into the finance "
        "store. Give the file path and an account label (something stable "
        "like chase_checking, amex, or vq_paypal). The same file is never "
        "imported twice. Pass flip_amount_sign=true if the CSV lists "
        "charges as positive numbers, so money out stays negative. "
        "Imported transactions are uncategorized; categorize them next."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the statement CSV file.",
            },
            "account": {
                "type": "string",
                "description": "A stable account label, e.g. chase_checking.",
            },
            "flip_amount_sign": {
                "type": "boolean",
                "description": (
                    "True if the file lists charges as positive. Optional, "
                    "defaults false."
                ),
            },
        },
        "required": ["path", "account"],
    },
    handler=_import_statement,
)

_CASH_FLOW = ToolSpec(
    name="cash_flow",
    description=(
        "Model cash flow for a period: income, expenses, the net, and "
        "breakdowns by stream and category. Give a year and month, or a "
        "start and end ISO date. This is also how you build the monthly "
        "P&L. Expense figures come back as positive magnitudes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "Year, with month."},
            "month": {"type": "integer", "description": "Month 1-12."},
            "start": {"type": "string", "description": "ISO start date."},
            "end": {"type": "string", "description": "ISO end date."},
        },
        "required": [],
    },
    handler=_cash_flow,
)

_LIST_TRANSACTIONS = ToolSpec(
    name="list_transactions",
    description=(
        "List individual transactions for a date range, newest first, so "
        "you can see the line items behind a number. Filter by account or "
        "stream. start and end are required ISO dates."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "ISO start date."},
            "end": {"type": "string", "description": "ISO end date."},
            "account": {"type": "string", "description": "Account filter."},
            "stream": {"type": "string", "description": "Stream filter."},
            "limit": {
                "type": "integer",
                "description": "Max rows, 1-500. Default 50.",
            },
        },
        "required": ["start", "end"],
    },
    handler=_list_transactions,
)

_CATEGORIZE = ToolSpec(
    name="categorize",
    description=(
        "Assign a category and/or stream to transactions. Set one "
        "transaction by transaction_id, or every transaction whose "
        "description contains a match string (e.g. match 'KROGER' for "
        "groceries). Streams are w2, side_shop, tutoring, household. "
        "By default a match only touches uncategorized transactions; pass "
        "only_uncategorized=false to recategorize."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "transaction_id": {
                "type": "string",
                "description": "A single transaction's id.",
            },
            "match": {
                "type": "string",
                "description": "Description substring to match (case-insensitive).",
            },
            "category": {
                "type": "string",
                "description": "Spending category, e.g. groceries, fuel.",
            },
            "stream": {
                "type": "string",
                "description": "w2, side_shop, tutoring, or household.",
            },
            "only_uncategorized": {
                "type": "boolean",
                "description": "For match mode. Default true.",
            },
        },
        "required": [],
    },
    handler=_categorize,
)

_ADD_RECURRING_BILL = ToolSpec(
    name="add_recurring_bill",
    description=(
        "Add a recurring bill to watch: ACA premium, auto insurance, a "
        "SaaS subscription. Record the typical amount and cadence so you "
        "can tell when one is missing or has changed. cadence is weekly, "
        "monthly, quarterly, or annual."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The bill's name."},
            "amount": {
                "type": "string",
                "description": "Typical amount, positive.",
            },
            "cadence": {
                "type": "string",
                "description": "weekly, monthly, quarterly, or annual.",
            },
            "due_day": {
                "type": "integer",
                "description": "Day of month it is due, 1-31. Optional.",
            },
            "account": {"type": "string", "description": "Account it hits."},
            "stream": {"type": "string", "description": "Expense stream."},
            "notes": {"type": "string", "description": "Anything useful."},
        },
        "required": ["name", "amount"],
    },
    handler=_add_recurring_bill,
)

_LIST_RECURRING_BILLS = ToolSpec(
    name="list_recurring_bills",
    description=(
        "List recurring bills. Defaults to active bills. Pass status "
        "(active, paused, ended) to filter, or 'all' for every bill."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "active, paused, ended, or all. Default active.",
            }
        },
        "required": [],
    },
    handler=_list_recurring_bills,
)

_UPDATE_RECURRING_BILL = ToolSpec(
    name="update_recurring_bill",
    description=(
        "Update a recurring bill: change its amount, cadence, status, "
        "notes, or last_seen date. Use status 'ended' when a bill stops, "
        "'paused' when it is on hold. The bill_id may be a full id or its "
        "first 8+ characters from list_recurring_bills."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bill_id": {
                "type": "string",
                "description": "Bill id, or its first 8+ characters.",
            },
            "amount": {"type": "string", "description": "New typical amount."},
            "cadence": {
                "type": "string",
                "description": "weekly, monthly, quarterly, or annual.",
            },
            "status": {
                "type": "string",
                "description": "active, paused, or ended.",
            },
            "last_seen": {
                "type": "string",
                "description": "ISO date a matching charge was last found.",
            },
            "notes": {"type": "string", "description": "Updated notes."},
        },
        "required": ["bill_id"],
    },
    handler=_update_recurring_bill,
)

_DETECT_ANOMALIES = ToolSpec(
    name="detect_anomalies",
    description=(
        "Flag spending anomalies for a month: categories that spiked "
        "against the trailing three-month average, and unusually large "
        "single charges. Run this when closing a month. Report the "
        "comparison, not just the number."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "Year."},
            "month": {"type": "integer", "description": "Month 1-12."},
        },
        "required": ["year", "month"],
    },
    handler=_detect_anomalies,
)

_LOG_DECISION = ToolSpec(
    name="log_decision",
    description=(
        "Record a financial decision Trilok has made, in the decision "
        "journal: a price set, a recurring cost cut, a purchase committed. "
        "Capture the rationale and the outcome expected, and set review_at "
        "to when the call should be revisited."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "What was decided, in one line.",
            },
            "rationale": {
                "type": "string",
                "description": "Why it was decided that way. Optional.",
            },
            "expected_outcome": {
                "type": "string",
                "description": "What you expect to result. Optional.",
            },
            "domain": {
                "type": "string",
                "description": "Area it belongs to. Defaults to money.",
            },
            "review_at": {
                "type": "string",
                "description": "When to revisit, ISO date YYYY-MM-DD. Optional.",
            },
        },
        "required": ["summary"],
    },
    handler=_log_decision,
)


def build_tools() -> list[ToolSpec]:
    """Return the CFO's current toolset. Called once per dispatch."""
    return [
        _IMPORT_STATEMENT,
        _PORTFOLIO,
        _CASH_FLOW,
        _LIST_TRANSACTIONS,
        _CATEGORIZE,
        _ADD_RECURRING_BILL,
        _LIST_RECURRING_BILLS,
        _UPDATE_RECURRING_BILL,
        _DETECT_ANOMALIES,
        _LOG_DECISION,
        recall_facts_tool(),
        remember_fact_tool("cfo"),
        recall_lessons_tool(),
    ]


__all__ = ["build_tools"]
