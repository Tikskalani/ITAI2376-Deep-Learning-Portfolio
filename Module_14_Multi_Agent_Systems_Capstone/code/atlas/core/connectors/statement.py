"""Atlas -- the statement CSV connector.

The first concrete connector built on the Connector base. It reads a bank
or credit-card statement exported as CSV and returns a list of normalized
transactions. Phase 1 keeps statement ingestion manual: Trilok exports a
CSV, drops it in a folder, and the CFO agent imports it through this
connector.

Banks and card issuers do not agree on a CSV layout, so the connector
auto-detects columns from the header row, with a constructor override for
the cases auto-detection cannot guess. Two layouts are handled:

  * a single signed amount column ("Amount"), and
  * a split debit/credit pair ("Withdrawals" / "Deposits").

Sign convention, applied uniformly so the CFO never has to second-guess:
a negative amount is money leaving the account, a positive amount is
money coming in. A single amount column is taken as the export already
signed it; pass flip_amount_sign=True for a card export that lists
charges as positive. In a split layout, a debit is negative and a credit
is positive.

A row that cannot be parsed (a bad date, a junk amount) raises rather
than being silently dropped. The CFO money path has to be honest; a
quietly skipped transaction is a wrong cash-flow number later.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from core.connectors.base import Connector

# Header aliases, lowercased. The first matching header in the file wins.
_DATE_ALIASES = (
    "date",
    "transaction date",
    "trans date",
    "posting date",
    "posted date",
    "post date",
)
_DESCRIPTION_ALIASES = (
    "description",
    "name",
    "payee",
    "memo",
    "details",
    "transaction",
    "narrative",
)
_AMOUNT_ALIASES = (
    "amount",
    "transaction amount",
)
_DEBIT_ALIASES = (
    "debit",
    "withdrawal",
    "withdrawals",
    "money out",
    "paid out",
)
_CREDIT_ALIASES = (
    "credit",
    "deposit",
    "deposits",
    "money in",
    "paid in",
)

# Date formats tried in order. ISO first, then US layouts (Trilok is in TX).
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
)


class StatementTransaction(BaseModel):
    """One normalized line from a statement.

    posted      -- the transaction date
    description -- the cleaned description / payee text
    amount      -- signed: negative is money out, positive is money in
    raw         -- the original CSV row, header -> value, kept for audit
    """

    model_config = ConfigDict(frozen=True)

    posted: date
    description: str
    amount: Decimal
    raw: dict[str, str]


class StatementColumns(BaseModel):
    """An explicit column mapping, to override auto-detection.

    Give either `amount` (a single signed column) or the `debit`/`credit`
    pair, not both. Names must match the CSV header exactly.
    """

    date: str
    description: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None


def _parse_date(raw: str) -> date:
    """Parse a date cell, trying each known format."""
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {raw!r}")


def _parse_amount(raw: str) -> Decimal:
    """Parse a money cell. Handles $, thousands commas, and ( ) negatives."""
    text = raw.strip()
    if not text:
        return Decimal("0")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if not text:
        return Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"unrecognized amount {raw!r}") from exc
    return -value if negative else value


def _match_header(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    """Return the first real header whose lowercased form is in aliases."""
    lowered = {h.strip().lower(): h for h in headers}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def _detect_columns(headers: list[str]) -> StatementColumns:
    """Work out which columns hold date, description, and amount(s)."""
    date_col = _match_header(headers, _DATE_ALIASES)
    desc_col = _match_header(headers, _DESCRIPTION_ALIASES)
    if date_col is None:
        raise ValueError(
            f"no date column found in headers {headers!r}; "
            "pass an explicit StatementColumns mapping"
        )
    if desc_col is None:
        raise ValueError(
            f"no description column found in headers {headers!r}; "
            "pass an explicit StatementColumns mapping"
        )

    amount_col = _match_header(headers, _AMOUNT_ALIASES)
    if amount_col is not None:
        return StatementColumns(
            date=date_col, description=desc_col, amount=amount_col
        )

    debit_col = _match_header(headers, _DEBIT_ALIASES)
    credit_col = _match_header(headers, _CREDIT_ALIASES)
    if debit_col is None and credit_col is None:
        raise ValueError(
            f"no amount column (single or debit/credit) found in headers "
            f"{headers!r}; pass an explicit StatementColumns mapping"
        )
    return StatementColumns(
        date=date_col,
        description=desc_col,
        debit=debit_col,
        credit=credit_col,
    )


def _row_amount(
    row: dict[str, str], cols: StatementColumns, flip: bool
) -> Decimal:
    """Pull the signed amount from one row, single-column or split."""
    if cols.amount is not None:
        value = _parse_amount(row.get(cols.amount, ""))
        return -value if flip else value

    debit = (
        _parse_amount(row.get(cols.debit, ""))
        if cols.debit is not None
        else Decimal("0")
    )
    credit = (
        _parse_amount(row.get(cols.credit, ""))
        if cols.credit is not None
        else Decimal("0")
    )
    # Debit is money out (negative), credit is money in (positive). Either
    # column may itself be signed already, so normalize by magnitude.
    return abs(credit) - abs(debit)


class StatementConnector(Connector):
    """Reads a bank or card statement CSV into normalized transactions.

    The key passed to `get` is the path to the CSV file. The result's
    `data` is a list[StatementTransaction], oldest-first as the file
    lists them.
    """

    name = "statement_csv"
    # Files can be re-exported under the same path; keep the cache short.
    cache_ttl_seconds = 60.0

    def __init__(
        self,
        columns: StatementColumns | None = None,
        *,
        flip_amount_sign: bool = False,
    ) -> None:
        """columns overrides header auto-detection; flip_amount_sign negates
        a single-column amount, for card exports that list charges as
        positive."""
        super().__init__()
        self._columns = columns
        self._flip = flip_amount_sign

    async def _fetch_fresh(self, key: str) -> list[StatementTransaction]:
        """Read and normalize the CSV at path `key`.

        The read and parse are synchronous work, run off the event loop so
        a large statement does not block other agents.
        """
        return await asyncio.to_thread(self._parse_file, key)

    def _parse_file(self, key: str) -> list[StatementTransaction]:
        """Synchronous read + normalize. Runs in a worker thread."""
        path = Path(key)
        if not path.is_file():
            raise FileNotFoundError(f"statement file not found: {key}")

        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(text.splitlines())
        if reader.fieldnames is None:
            raise ValueError(f"statement file is empty: {key}")

        headers = [h for h in reader.fieldnames if h is not None]
        cols = self._columns or _detect_columns(headers)

        transactions: list[StatementTransaction] = []
        for line_no, row in enumerate(reader, start=2):  # row 1 is the header
            # Skip a fully blank line (a trailing newline is common).
            if not any((v or "").strip() for v in row.values()):
                continue
            try:
                posted = _parse_date(row.get(cols.date, ""))
                amount = _row_amount(row, cols, self._flip)
            except ValueError as exc:
                raise ValueError(f"row {line_no}: {exc}") from exc
            description = (row.get(cols.description, "") or "").strip()
            transactions.append(
                StatementTransaction(
                    posted=posted,
                    description=description,
                    amount=amount,
                    raw={k: (v or "") for k, v in row.items() if k is not None},
                )
            )
        return transactions


__all__ = [
    "StatementConnector",
    "StatementColumns",
    "StatementTransaction",
]
