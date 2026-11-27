"""Which privacy rule each of your last 50 inbox messages matches.

  python -m scripts.exclusion_preview              your real inbox (headers and labels only)
  python -m scripts.exclusion_preview --fixture F  a JSON file of messages instead (tests)

RUN THIS IN YOUR OWN TERMINAL. Do not paste its output into any chat: sender
domains and label matches about your clients are exactly what the privacy
gate keeps from models.

It makes read-only Gmail API calls (messages.list on the inbox, messages.get
with format=metadata, labels.list): no bodies are fetched. For each message it
prints the sender's DOMAIN only (never an address or a subject) and the rule
label it matched, or "visible" when no rule did, then a count per label. The
content screen runs on the subject and Gmail's snippet locally; neither is
printed. It reads data/model_exclude.yaml, so edit that file and run it again
until the matches look right. Then turn mail model reads on (docs/privacy.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from core import privacy


def _hdr(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name:
            return str(h.get("value") or "")
    return ""


def classify(msg: dict[str, Any], label_names: dict[str, str] | None,
             rules: privacy.Rules | None = None) -> tuple[str, str]:
    """(sender domain, matched rule label or "visible") for one message."""
    sender = _hdr(msg, "from")
    domain = (parseaddr(sender)[1].rsplit("@", 1)[-1] or "?").lower()
    names = [(label_names or {}).get(i, i) for i in msg.get("labelIds") or []]
    hit = privacy.is_private_mail(sender, recipients=[_hdr(msg, "to"), _hdr(msg, "cc")], labels=names, rules=rules) \
        or privacy.screen(f"{_hdr(msg, 'subject')}\n{msg.get('snippet') or ''}", rules=rules)
    return domain, (hit.label if hit else "visible")


def report(rows: list[tuple[str, str]]) -> str:
    width = max([len(d) for d, _ in rows] + [13])
    out = [f"{'sender domain':<{width}}  rule", f"{'-' * width}  ----"]
    out += [f"{d:<{width}}  {label}" for d, label in rows]
    counts = Counter(label for _, label in rows)
    out += ["", "Per rule:"] + [f"  {label}: {n}" for label, n in counts.most_common()]
    out += ["", f"Model reads are {'ON' if privacy.mail_model_reads() else 'OFF (safe-start)'}."]
    return "\n".join(out)


async def from_gmail(limit: int = 50) -> list[tuple[str, str]]:
    from core import db
    from core.connectors.gmail import GmailConnector
    from core.connectors.google_auth import configured

    if not configured():
        raise SystemExit("Google is not connected. Run scripts\\connect.bat first.")
    conn = GmailConnector()
    try:
        names = await conn.label_names()
        listing = await conn._get("/messages", {"q": "in:inbox", "maxResults": limit})
        rows = []
        for m in listing.get("messages") or []:
            msg = await conn._get(f"/messages/{m['id']}", {
                "format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Subject"]})
            rows.append(classify(msg, names))
        return rows
    finally:
        await db.close_pool()


def from_fixture(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [classify(m, data.get("label_names")) for m in data.get("messages") or []]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", type=Path, help="a JSON file {label_names, messages} instead of Gmail")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args(argv)
    rows = from_fixture(args.fixture) if args.fixture else asyncio.run(from_gmail(args.limit))
    print(report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
