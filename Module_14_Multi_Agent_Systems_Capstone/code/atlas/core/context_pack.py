"""Atlas -- the context pack: who matters, what is noise, what is open.

config/context.yaml holds what Trilok's mail history taught Atlas: the
people he actually writes to (with a tier and a project), the senders he
never opens, a few triage rules, and the open loops per project as of a
snapshot date. This module reads it once per process (re-read when the file
changes) and hands three consumers what they need:

  people_block() / open_loops_block()  the CEO's context
  owes_block()                         promises Trilok owes, appended to every brief
  triage_block()                       appended to the triage rubric
  is_noise() / sender_hint()           the Gmail connector, before a signal
                                       is written (noise never costs a token)

A missing or broken file is not an error: every function degrades to empty.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, date, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import yaml

from core.config import REPO_ROOT

log = logging.getLogger(__name__)

PATH = REPO_ROOT / "config" / "context.yaml"
# Written by core.mail_learning on a schedule; gitignored, regenerated at will.
LEARNED_PATH = REPO_ROOT / "data" / "context.learned.yaml"
# Loops Trilok marked Done from a brief. Its own file, so mail learning
# rewriting LEARNED_PATH never brings a closed loop back.
CLOSED_PATH = REPO_ROOT / "data" / "context.closed.yaml"

_cache: dict[str, Any] = {"key": None, "data": {}}


def _parse(p: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
        return data
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001 - a bad edit must not take Atlas down
        log.warning("context pack %s unreadable: %s", p, exc)
        return {}


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def merge(manual: dict[str, Any], learned: dict[str, Any]) -> dict[str, Any]:
    """Hand-written wins; learned fills in.

    People: every hand-written person, then learned people whose address
    or domain no hand-written entry covers. Noise: the union, minus anything
    that would silence a known person. Rules: hand-written only. Open loops:
    the union by project, person and text (a learned refresh adds, it never
    erases a hand-written loop; Done in a brief closes one), dated by the
    newer of the two."""
    if not learned:
        return manual
    out = dict(manual)
    people = list(manual.get("people") or [])
    for lp in learned.get("people") or []:
        emails = lp.get("emails") or []
        if not any(sender_hint(e, {"people": people}) for e in emails):
            people.append(lp)
    out["people"] = people
    mn, ln = manual.get("noise") or {}, learned.get("noise") or {}
    out["noise"] = {
        "domains": sorted(set(mn.get("domains") or []) | set(ln.get("domains") or [])),
        "addresses": sorted(set(mn.get("addresses") or []) | set(ln.get("addresses") or [])),
    }
    if learned.get("open_loops") is not None:
        merged: dict[str, list[dict[str, Any]]] = {}
        seen: set[str] = set()
        for source in (manual.get("open_loops") or {}, learned.get("open_loops") or {}):
            for slug, items in source.items():
                for it in items or []:
                    key = loop_key(slug, it)
                    if key not in seen:
                        seen.add(key)
                        merged.setdefault(slug, []).append(it)
        out["open_loops"] = merged
        loops_at = str(learned.get("open_loops_at") or "")[:10]
        out["snapshot"] = max(loops_at, str(manual.get("snapshot") or ""))
    return out


def loop_key(project: str, item: dict[str, Any]) -> str:
    """A loop's identity: its project, person and text, case and spacing ignored."""
    norm = " ".join(f"{project}|{item.get('who') or ''}|{item.get('text') or ''}".lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _closed_keys() -> set[str]:
    return {str(c.get("key")) for c in (_parse(CLOSED_PATH).get("closed") or []) if isinstance(c, dict)}


def close_loop(key: str) -> dict[str, Any] | None:
    """Mark an open loop Done. Returns it, or None when no open loop has that key."""
    for slug, items in (load().get("open_loops") or {}).items():
        for it in items or []:
            if loop_key(slug, it) == key:
                closed = list(_parse(CLOSED_PATH).get("closed") or [])
                entry = {"key": key, "project": slug, "who": it.get("who"), "text": it.get("text"),
                         "closed_at": datetime.now(UTC).replace(microsecond=0).isoformat()}
                closed.append(entry)
                CLOSED_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = CLOSED_PATH.with_suffix(".tmp")
                tmp.write_text(yaml.safe_dump({"closed": closed}, sort_keys=False, allow_unicode=True), encoding="utf-8")
                os.replace(tmp, CLOSED_PATH)
                return entry
    return None


def load(path: Path | None = None) -> dict[str, Any]:
    """The context pack: config/context.yaml merged over what Atlas learned
    from mail, cached until either file changes. With `path`, just that file."""
    if path is not None:
        return _parse(path)
    key = (_mtime(PATH), _mtime(LEARNED_PATH), _mtime(CLOSED_PATH))
    if _cache["key"] == key:
        return _cache["data"]
    data = merge(_parse(PATH), _parse(LEARNED_PATH))
    closed = _closed_keys()
    if closed:
        data = {**data, "open_loops": {
            slug: [it for it in items or [] if loop_key(slug, it) not in closed]
            for slug, items in (data.get("open_loops") or {}).items()}}
    _cache.update(key=key, data=data)
    return data


def _addr(sender: str) -> str:
    return (parseaddr(sender or "")[1] or sender or "").strip().lower()


def _domain_match(domain: str, rule: str) -> bool:
    rule = rule.lower().lstrip("@")
    return domain == rule or domain.endswith("." + rule)


def is_noise(sender: str, data: dict[str, Any] | None = None) -> bool:
    """True when the sender is on the never-surface list."""
    d = load() if data is None else data
    noise = d.get("noise") or {}
    addr = _addr(sender)
    if not addr:
        return False
    if sender_hint(sender, d):  # a known person always gets through
        return False
    rules = [str(x).lower() for x in (noise.get("domains") or [])] + \
            [str(x).lower() for x in (noise.get("addresses") or [])]
    domain = addr.rsplit("@", 1)[-1]
    for r in rules:
        if "@" in r.lstrip("@"):
            if addr == r:
                return True
        elif _domain_match(domain, r):
            return True
    return False


def sender_hint(sender: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """{name, tier, project} for a known sender, else None. Exact address
    beats domain."""
    d = load() if data is None else data
    addr = _addr(sender)
    if not addr:
        return None
    domain = addr.rsplit("@", 1)[-1]
    by_domain = None
    for person in d.get("people") or []:
        emails = [str(e).lower() for e in person.get("emails") or []]
        hint = {"name": person.get("name"), "tier": person.get("tier", 3),
                "project": person.get("project")}
        if addr in emails:
            return hint
        if by_domain is None and any(_domain_match(domain, str(x)) for x in person.get("domains") or []):
            by_domain = hint
    return by_domain


def people_block(data: dict[str, Any] | None = None) -> str:
    """One line per person, tier order. For the CEO's context."""
    d = load() if data is None else data
    people = sorted(d.get("people") or [], key=lambda p: (p.get("tier", 3), p.get("name", "")))
    lines = []
    for p in people:
        bits = [f"tier {p.get('tier', 3)}"]
        if p.get("project"):
            bits.append(p["project"])
        if p.get("org"):
            bits.append(p["org"])
        line = f"- {p.get('name')} [{' | '.join(bits)}]"
        if p.get("note"):
            line += f": {p['note']}"
        lines.append(line)
    return "\n".join(lines)


def _excluded(it: dict[str, Any]) -> bool:
    """A loop with a correspondent the privacy gate excludes (core/privacy.py)."""
    from core import privacy

    return bool(it.get("addr")) and privacy.is_private_mail(str(it["addr"])) is not None


def open_loops_block(data: dict[str, Any] | None = None) -> str:
    """Open loops per project, as of the snapshot date. This goes into the
    CEO's context (a model), so the privacy gate applies: loops with an
    excluded correspondent are left out, and loops read from Gmail are only
    counted while mail model reads are off."""
    from core import privacy

    d = load() if data is None else data
    loops = d.get("open_loops") or {}
    if not loops:
        return ""
    reads = privacy.mail_model_reads()
    lines = []
    hidden = 0
    for slug, items in loops.items():
        for it in items or []:
            if _excluded(it):
                continue
            if it.get("mail") and not reads:
                hidden += 1
                continue
            owner = "Trilok owes" if it.get("owner") == "me" else "waiting on"
            since = f" (since {it['since']})" if it.get("since") else ""
            lines.append(f"- {slug}: {owner} {it.get('who', '?')}{since}: {it.get('text', '')}")
    if hidden:
        lines.append(f"- {hidden} more open email thread{'s' if hidden != 1 else ''}, not read by a model (safe-start)")
    if not lines:
        return ""
    head = f"Open loops as of {d.get('snapshot', 'the last snapshot')}{_age_note(d)} (from email; newer signals override):"
    return head + "\n" + "\n".join(lines)


# The open loops go stale when nothing refreshes them (mail learning needs
# Gmail). Past this age every brief says so.
LOOPS_STALE_DAYS = 7


def loops_age_days(data: dict[str, Any] | None = None, today: date | None = None) -> int | None:
    """Days since the open loops were last written, or None without a date."""
    d = load() if data is None else data
    try:
        snap = date.fromisoformat(str(d.get("snapshot") or "")[:10])
    except ValueError:
        return None
    return ((today or date.today()) - snap).days


def _age_note(d: dict[str, Any], today: date | None = None) -> str:
    age = loops_age_days(d, today)
    return "" if age is None else f", {age} day{'s' if age != 1 else ''} old"


def owed(data: dict[str, Any] | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Open loops where Trilok owes someone and the promise is at least a day
    old, oldest first. Every project counts the same: a promise is not ranked."""
    d = load() if data is None else data
    today = today or date.today()
    out = []
    for slug, items in (d.get("open_loops") or {}).items():
        for it in items or []:
            if it.get("owner") != "me" or _excluded(it):
                continue
            try:
                since = date.fromisoformat(str(it.get("since") or "")[:10])
            except ValueError:
                since = None
            if since is not None and since >= today:
                continue
            out.append({"project": slug, "who": it.get("who") or "?", "since": since, "text": it.get("text") or "",
                        "key": loop_key(slug, it)})
    return sorted(out, key=lambda o: (o["since"] or date.min, o["project"]))


def owes_block(data: dict[str, Any] | None = None, today: date | None = None) -> str:
    """Overdue promises, one line each, with the list's age; a warning when
    it is older than LOOPS_STALE_DAYS. Empty when there is nothing to say."""
    d = load() if data is None else data
    today = today or date.today()
    items = owed(d, today)
    age = loops_age_days(d, today)
    lines: list[str] = []
    if items:
        lines.append(f"Promises you owe ({len(items)}, from the open loops as of {d.get('snapshot')}):")
        for o in items:
            since = f", since {o['since']:%b} {o['since'].day}" if o["since"] else ""
            lines.append(f"- {o['who']} ({o['project']}{since}): {o['text']}")
    if age is not None and age > LOOPS_STALE_DAYS:
        lines.append(f"These open loops are {age} days old. They refresh from Gmail each morning once Gmail is "
                     "connected; until then, edit config/context.yaml.")
    return "\n".join(lines)


def triage_block(data: dict[str, Any] | None = None) -> str:
    """People and rules appended to the triage rubric (prompt-cached)."""
    d = load() if data is None else data
    parts = []
    people = people_block(d)
    if people:
        parts.append("Known people (a signal's `known` field names the match):\n" + people)
    rules = d.get("rules") or []
    if rules:
        parts.append("Trilok's rules:\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


__all__ = ["CLOSED_PATH", "LEARNED_PATH", "LOOPS_STALE_DAYS", "PATH", "close_loop", "is_noise", "load",
           "loop_key", "loops_age_days", "merge",
           "open_loops_block", "owed", "owes_block", "people_block", "sender_hint", "triage_block"]
