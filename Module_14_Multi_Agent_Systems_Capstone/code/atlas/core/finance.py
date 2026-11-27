"""Atlas -- CFO finance data layer.

The store behind the CFO agent: imported bank and card statements, the
transactions they hold, and the recurring bills the CFO watches. It sits
on top of migration 003.

Two ideas keep the money numbers honest:

  * Import dedupe. Every statement file is hashed; importing the same file
    twice is caught by the unique content_hash, so a month never silently
    double-counts.
  * One sign convention. Every amount is signed the same way the statement
    connector signs it: negative is money leaving, positive is money in.
    Cash-flow and anomaly math rely on that and never have to guess.

Functions here are plain data access. Judgment (what a number means, what
to tell Trilok) lives in the CFO agent, not here. The anomaly math is the
one exception: it is deterministic and testable, so the arithmetic lives
in pure helpers and only the row fetching touches the database.
"""

from __future__ import annotations

import calendar
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from core import db

_TXN_COLS = (
    "id, import_id, account, posted, description, amount, category, "
    "stream, created_at, metadata"
)
_BILL_COLS = (
    "id, name, amount, cadence, due_day, account, stream, status, "
    "last_seen, notes, created_at, metadata"
)

# Anomaly thresholds. A spike has to clear both a ratio and an absolute
# bar, so a category going from $2 to $5 does not raise a flag.
_SPIKE_RATIO = Decimal("1.5")
_SPIKE_MIN_ABS = Decimal("50")
_NEW_CATEGORY_MIN = Decimal("100")
_LARGE_TXN_FLOOR = Decimal("150")
_LARGE_TXN_RATIO = Decimal("2")

_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_file(path: str) -> str:
    """Return the sha256 hex digest of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def month_range(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) for a calendar month."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Move (year, month) by delta months."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


# ---------------------------------------------------------------------------
# Statement imports
# ---------------------------------------------------------------------------


async def find_import_by_hash(content_hash: str) -> dict[str, Any] | None:
    """Return the import row for a file hash, or None if not yet imported."""
    row = await db.fetchrow(
        "select id, account, source_file, content_hash, row_count, "
        "imported_at from statement_imports where content_hash = $1",
        content_hash,
    )
    return dict(row) if row else None


async def record_import(
    account: str,
    source_file: str,
    content_hash: str,
    row_count: int,
) -> UUID:
    """Record one statement import. Returns its id."""
    row = await db.fetchrow(
        """
        insert into statement_imports
          (account, source_file, content_hash, row_count)
        values ($1, $2, $3, $4)
        returning id
        """,
        account,
        source_file,
        content_hash,
        row_count,
    )
    return row["id"]


async def add_transactions(
    import_id: UUID,
    account: str,
    rows: list[dict[str, Any]],
) -> int:
    """Bulk-insert transactions for one import.

    Each row needs posted (date), description (str), amount (Decimal), and
    an optional metadata dict. Returns the number of rows inserted.
    """
    if not rows:
        return 0
    posted = [r["posted"] for r in rows]
    descriptions = [r["description"] for r in rows]
    amounts = [r["amount"] for r in rows]
    metadata = [r.get("metadata") or {} for r in rows]
    await db.execute(
        """
        insert into transactions
          (import_id, account, posted, description, amount, metadata)
        select $1, $2, p, d, a, m
        from unnest($3::date[], $4::text[], $5::numeric[], $6::jsonb[])
          as t(p, d, a, m)
        """,
        import_id,
        account,
        posted,
        descriptions,
        amounts,
        metadata,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Transaction queries
# ---------------------------------------------------------------------------


async def list_transactions(
    start: date,
    end: date,
    account: str | None = None,
    stream: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List transactions posted in [start, end], newest first."""
    clauses = ["posted between $1 and $2"]
    args: list[Any] = [start, end]
    if account:
        args.append(account)
        clauses.append(f"account = ${len(args)}")
    if stream:
        args.append(stream)
        clauses.append(f"stream = ${len(args)}")
    args.append(limit)
    rows = await db.fetch(
        f"select {_TXN_COLS} from transactions "
        f"where {' and '.join(clauses)} "
        f"order by posted desc, created_at desc limit ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]


async def cash_flow(start: date, end: date) -> dict[str, Any]:
    """Aggregate cash flow over [start, end].

    Returns income and expense totals, the net, and breakdowns by stream
    and by spending category. Expense figures are positive magnitudes.
    """
    totals = await db.fetchrow(
        """
        select
          coalesce(sum(amount) filter (where amount > 0), 0) as income,
          coalesce(sum(-amount) filter (where amount < 0), 0) as expense,
          count(*) as n
        from transactions
        where posted between $1 and $2
        """,
        start,
        end,
    )
    stream_rows = await db.fetch(
        """
        select coalesce(stream, 'uncategorized') as stream,
          coalesce(sum(amount) filter (where amount > 0), 0) as income,
          coalesce(sum(-amount) filter (where amount < 0), 0) as expense
        from transactions
        where posted between $1 and $2
        group by 1
        order by 1
        """,
        start,
        end,
    )
    category_rows = await db.fetch(
        """
        select coalesce(category, 'uncategorized') as category,
          coalesce(sum(-amount), 0) as expense
        from transactions
        where posted between $1 and $2 and amount < 0
        group by 1
        order by 2 desc
        """,
        start,
        end,
    )
    income = totals["income"]
    expense = totals["expense"]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "income_total": income,
        "expense_total": expense,
        "net": income - expense,
        "transaction_count": totals["n"],
        "by_stream": {
            r["stream"]: {
                "income": r["income"],
                "expense": r["expense"],
                "net": r["income"] - r["expense"],
            }
            for r in stream_rows
        },
        "by_category": {r["category"]: r["expense"] for r in category_rows},
    }


async def categorize_by_id(
    transaction_id: UUID,
    category: str | None = None,
    stream: str | None = None,
) -> bool:
    """Set the category and/or stream on one transaction by id."""
    if category is None and stream is None:
        raise ValueError("give a category, a stream, or both")
    result = await db.execute(
        """
        update transactions
        set category = coalesce($2, category),
            stream = coalesce($3, stream)
        where id = $1
        """,
        transaction_id,
        category,
        stream,
    )
    return not result.endswith(" 0")


async def categorize_by_match(
    match: str,
    category: str | None = None,
    stream: str | None = None,
    only_uncategorized: bool = True,
) -> int:
    """Categorize every transaction whose description contains `match`.

    Case-insensitive substring match. With only_uncategorized True (the
    default) it leaves already-categorized rows alone. Returns the count
    of rows changed.
    """
    if category is None and stream is None:
        raise ValueError("give a category, a stream, or both")
    if not match.strip():
        raise ValueError("match cannot be empty")
    clauses = ["description ilike $1"]
    args: list[Any] = [f"%{match}%", category, stream]
    if only_uncategorized:
        clauses.append("category is null and stream is null")
    result = await db.execute(
        f"""
        update transactions
        set category = coalesce($2, category),
            stream = coalesce($3, stream)
        where {' and '.join(clauses)}
        """,
        *args,
    )
    # execute returns "UPDATE <n>"
    return int(result.split()[-1]) if result.startswith("UPDATE") else 0


# ---------------------------------------------------------------------------
# Recurring bills
# ---------------------------------------------------------------------------


async def add_recurring_bill(
    name: str,
    amount: Decimal,
    cadence: str = "monthly",
    due_day: int | None = None,
    account: str | None = None,
    stream: str | None = None,
    notes: str | None = None,
) -> UUID:
    """Add a recurring bill for the CFO to watch. Returns its id."""
    row = await db.fetchrow(
        """
        insert into recurring_bills
          (name, amount, cadence, due_day, account, stream, notes)
        values ($1, $2, $3, $4, $5, $6, $7)
        returning id
        """,
        name,
        amount,
        cadence,
        due_day,
        account,
        stream,
        notes,
    )
    return row["id"]


async def list_recurring_bills(
    status: str | None = "active",
) -> list[dict[str, Any]]:
    """List recurring bills. status=None returns every bill."""
    if status is None:
        rows = await db.fetch(
            f"select {_BILL_COLS} from recurring_bills order by name"
        )
    else:
        rows = await db.fetch(
            f"select {_BILL_COLS} from recurring_bills "
            "where status = $1 order by name",
            status,
        )
    return [dict(r) for r in rows]


async def find_recurring_bill(raw: str) -> dict[str, Any] | None:
    """Resolve a bill by full UUID or an 8+ character id prefix."""
    try:
        uid: UUID | None = UUID(raw)
    except ValueError:
        uid = None
    if uid is not None:
        row = await db.fetchrow(
            f"select {_BILL_COLS} from recurring_bills where id = $1", uid
        )
        return dict(row) if row else None
    if len(raw) < 8:
        return None
    rows = await db.fetch(
        f"select {_BILL_COLS} from recurring_bills "
        "where id::text like $1 limit 2",
        f"{raw}%",
    )
    return dict(rows[0]) if len(rows) == 1 else None


_BILL_UPDATABLE = (
    "amount",
    "cadence",
    "due_day",
    "account",
    "stream",
    "status",
    "last_seen",
    "notes",
)


async def update_recurring_bill(bill_id: UUID, **fields: Any) -> bool:
    """Update one bill's mutable fields. Returns True if a row changed."""
    sets = {k: v for k, v in fields.items() if k in _BILL_UPDATABLE}
    if not sets:
        raise ValueError(
            f"nothing to update; allowed fields: {_BILL_UPDATABLE}"
        )
    assignments = []
    args: list[Any] = [bill_id]
    for col, value in sets.items():
        args.append(value)
        assignments.append(f"{col} = ${len(args)}")
    result = await db.execute(
        f"update recurring_bills set {', '.join(assignments)} where id = $1",
        *args,
    )
    return not result.endswith(" 0")


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def _category_swings(
    current: dict[str, Decimal],
    trailing_avg: dict[str, Decimal],
) -> list[dict[str, Any]]:
    """Flag categories that spiked against their trailing average.

    A spike clears both a ratio and an absolute bar. A category with no
    trailing history is flagged as new only if it is material.
    """
    flags: list[dict[str, Any]] = []
    for category, amount in current.items():
        avg = trailing_avg.get(category, _ZERO)
        if avg <= _ZERO:
            if amount >= _NEW_CATEGORY_MIN:
                flags.append(
                    {
                        "category": category,
                        "current": amount,
                        "trailing_avg": _ZERO,
                        "kind": "new",
                    }
                )
            continue
        if amount > avg * _SPIKE_RATIO and (amount - avg) > _SPIKE_MIN_ABS:
            flags.append(
                {
                    "category": category,
                    "current": amount,
                    "trailing_avg": avg,
                    "kind": "spike",
                }
            )
    flags.sort(
        key=lambda f: f["current"] - f["trailing_avg"], reverse=True
    )
    return flags


def _large_transactions(
    outflows: list[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Flag unusually large single outflows against the month's median.

    `outflows` rows each have description, amount (negative Decimal), and
    posted. The threshold is the larger of a fixed floor and twice the
    median magnitude, so a month of small charges does not flag noise.
    """
    if not outflows:
        return []
    magnitudes = sorted(abs(o["amount"]) for o in outflows)
    mid = len(magnitudes) // 2
    median = (
        magnitudes[mid]
        if len(magnitudes) % 2
        else (magnitudes[mid - 1] + magnitudes[mid]) / 2
    )
    threshold = max(_LARGE_TXN_FLOOR, median * _LARGE_TXN_RATIO)
    flagged = [o for o in outflows if abs(o["amount"]) >= threshold]
    flagged.sort(key=lambda o: abs(o["amount"]), reverse=True)
    return [
        {
            "description": o["description"],
            "amount": o["amount"],
            "posted": o["posted"],
        }
        for o in flagged[:limit]
    ]


async def detect_anomalies(year: int, month: int) -> dict[str, Any]:
    """Flag spending anomalies for one month.

    Compares each category's spend against the average of the prior three
    months and flags large single outflows. Pure arithmetic lives in the
    helpers; this function only fetches the rows.
    """
    cur_start, cur_end = month_range(year, month)
    trail_year, trail_month = _shift_month(year, month, -3)
    trail_start, _ = month_range(trail_year, trail_month)
    trail_end = date(cur_start.year, cur_start.month, 1)

    current_rows = await db.fetch(
        """
        select coalesce(category, 'uncategorized') as category,
               coalesce(sum(-amount), 0) as total
        from transactions
        where posted between $1 and $2 and amount < 0
        group by 1
        """,
        cur_start,
        cur_end,
    )
    trailing_rows = await db.fetch(
        """
        select coalesce(category, 'uncategorized') as category,
               coalesce(sum(-amount), 0) as total
        from transactions
        where posted >= $1 and posted < $2 and amount < 0
        group by 1
        """,
        trail_start,
        trail_end,
    )
    outflows = await db.fetch(
        """
        select description, amount, posted
        from transactions
        where posted between $1 and $2 and amount < 0
        """,
        cur_start,
        cur_end,
    )

    current = {r["category"]: r["total"] for r in current_rows}
    trailing_avg = {
        r["category"]: r["total"] / 3 for r in trailing_rows
    }
    return {
        "month": cur_start.strftime("%Y-%m"),
        "category_swings": _category_swings(current, trailing_avg),
        "large_transactions": _large_transactions(
            [dict(r) for r in outflows]
        ),
    }


__all__ = [
    "hash_file",
    "month_range",
    "find_import_by_hash",
    "record_import",
    "add_transactions",
    "list_transactions",
    "cash_flow",
    "categorize_by_id",
    "categorize_by_match",
    "add_recurring_bill",
    "list_recurring_bills",
    "find_recurring_bill",
    "update_recurring_bill",
    "detect_anomalies",
]
