"""Atlas -- learn who matters and what is open from Trilok's own mail.

The context pack (config/context.yaml) started as a hand analysis of six
months of Gmail. This module does the same analysis on a schedule, so it
stays current without anyone running it:

  weekly (Sunday 05:00)  learn(): the last 180 days of sent threads give the
                         people he actually corresponds with, ranked by
                         back-and-forth and recency; the last 30 days of
                         unread mail give the senders he never opens.
  daily (06:40)          refresh_open_loops(): threads from the last 21 days
                         where he owes a reply, or has waited three days or
                         more on someone else's, ready for the 08:00 brief.

Results go to data/context.learned.yaml (gitignored, regenerated at will).
core.context_pack merges it under the hand-written file: hand-written
people and rules win, learned people and noise are added, and learned open
loops replace the hand snapshot once they are newer.

Everything here is read-only against Gmail (metadata only: From, To, Cc,
Subject, dates). Nothing is sent, labelled or marked read.

The privacy gate (core/privacy.py): a thread with any excluded address
in it is skipped whole, so its people, subjects and loops are never
stored. The one model step (labelling correspondents by their subjects)
runs only while mail model reads are on.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

import yaml

from core.config import settings
from core.context_pack import LEARNED_PATH

log = logging.getLogger(__name__)

# Free-mail providers: a whole domain is never noise, only an address.
_PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
                     "icloud.com", "me.com", "live.com", "aol.com", "msn.com", "proton.me"}
_ROBOT = re.compile(r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?@|bounce)", re.I)


@dataclass
class Message:
    sender: str  # "Name <addr>"
    to: str = ""
    cc: str = ""
    subject: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Thread:
    id: str
    messages: list[Message]


def _addr(s: str) -> str:
    return (parseaddr(s or "")[1] or "").strip().lower()


def _name(s: str) -> str:
    return (parseaddr(s or "")[0] or "").strip().strip('"')


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def analyze(
    me: str,
    threads: list[Thread],
    unread_senders: list[str],
    *,
    now: datetime | None = None,
    my_name: str = "",
    loop_window_days: int = 21,
    wait_after_days: int = 3,
    owe_after_hours: int = 12,
    tier1_size: int = 10,
) -> dict[str, Any]:
    """Pure analysis. Returns {people, noise, open_loops_raw} where people
    are ranked correspondents, noise the never-opened senders, and
    open_loops_raw a list of {owner, who, addr, since, text, thread_id}."""
    now = now or datetime.now(UTC)
    me = me.lower()
    if not my_name:
        # His display name, from the From line of his own messages, so mail to
        # his other addresses ("Trilok Kalani <person2@yahoo.com>") is not a person.
        own = Counter(_name(m.sender) for t in threads for m in t.messages
                      if _addr(m.sender) == me and _name(m.sender))
        my_name = own.most_common(1)[0][0] if own else ""
    my_name = my_name.lower()

    def is_me(addr: str, name: str) -> bool:
        return addr == me or (bool(my_name) and name.lower() == my_name)

    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"mine": 0, "theirs": 0, "threads": 0, "last_at": None, "names": Counter(), "subjects": []}
    )
    loops: list[dict[str, Any]] = []
    from core import privacy

    rules = privacy.load()
    for t in threads:
        msgs = sorted(t.messages, key=lambda m: m.at)
        if not msgs:
            continue
        if any(privacy.is_private_mail(m.sender, recipients=[m.to, m.cc], rules=rules) for m in msgs):
            continue  # an excluded correspondent: nothing from this thread is kept
        seen: set[str] = set()
        subject = next((m.subject for m in msgs if m.subject), "(no subject)")
        for m in msgs:
            s_addr, s_name = _addr(m.sender), _name(m.sender)
            if is_me(s_addr, s_name):
                for name, addr in getaddresses([m.to, m.cc]):
                    a = addr.lower()
                    if not a or is_me(a, name) or _ROBOT.search(a):
                        continue
                    st = stats[a]
                    st["mine"] += 1
                    if name:
                        st["names"][name.strip('"')] += 1
                    seen.add(a)
                    st["last_at"] = max(filter(None, [st["last_at"], m.at]))
            elif s_addr and not _ROBOT.search(s_addr):
                st = stats[s_addr]
                st["theirs"] += 1
                if s_name:
                    st["names"][s_name] += 1
                seen.add(s_addr)
                st["last_at"] = max(filter(None, [st["last_at"], m.at]))
        for a in seen:
            stats[a]["threads"] += 1
            if len(stats[a]["subjects"]) < 3 and subject not in stats[a]["subjects"]:
                stats[a]["subjects"].append(subject[:80])

        last = msgs[-1]
        age = now - last.at
        if age > timedelta(days=loop_window_days):
            continue
        l_addr, l_name = _addr(last.sender), _name(last.sender)
        if is_me(l_addr, l_name):
            if age >= timedelta(days=wait_after_days):
                to = [(n, a.lower()) for n, a in getaddresses([last.to]) if a and not is_me(a.lower(), n)]
                if to:
                    n, a = to[0]
                    loops.append({"owner": "them", "who": n or a, "addr": a, "since": last.at.date().isoformat(),
                                  "text": subject[:160], "thread_id": t.id})
        elif l_addr and not _ROBOT.search(l_addr) and age >= timedelta(hours=owe_after_hours):
            loops.append({"owner": "me", "who": l_name or l_addr, "addr": l_addr,
                          "since": last.at.date().isoformat(), "text": subject[:160], "thread_id": t.id})

    # Rank people. Back-and-forth beats one-way; recency gates tier 1.
    ranked = []
    for addr, st in stats.items():
        if st["mine"] == 0:
            continue  # he never wrote to them: not a correspondent
        both = st["mine"] > 0 and st["theirs"] > 0
        recent = st["last_at"] is not None and now - st["last_at"] <= timedelta(days=45)
        ranked.append((both, recent, st["mine"] + st["theirs"], addr, st))
    ranked.sort(key=lambda r: (not r[0], -r[2]))
    people = []
    tier1_left = tier1_size
    for both, recent, _score, addr, st in ranked:
        if both and recent and tier1_left > 0:
            tier, tier1_left = 1, tier1_left - 1
        elif both:
            tier = 2
        elif st["mine"] >= 2:
            tier = 3
        else:
            continue
        name = st["names"].most_common(1)[0][0] if st["names"] else addr
        people.append({
            "name": name, "emails": [addr], "tier": tier,
            "note": f"{st['mine']} sent, {st['theirs']} received over {st['threads']} threads; "
                    f"last {st['last_at'].date().isoformat() if st['last_at'] else '?'}",
            "subjects": st["subjects"],
        })
        if len(people) >= 40:
            break

    # Noise: senders seen unread three times or more that he never writes to.
    corresponded = {a for a, st in stats.items() if st["mine"] > 0}
    corresponded_domains = {_domain(a) for a in corresponded}
    by_key: Counter[str] = Counter()
    for s in unread_senders:
        a = _addr(s)
        if not a or a in corresponded:
            continue
        d = _domain(a)
        key = a if d in _PERSONAL_DOMAINS else d
        if key in corresponded_domains:
            continue
        by_key[key] += 1
    noise_domains = sorted(k for k, c in by_key.items() if c >= 3 and "@" not in k)
    noise_addresses = sorted(k for k, c in by_key.items() if c >= 3 and "@" in k)

    loops.sort(key=lambda x: x["since"], reverse=True)
    return {"people": people, "noise": {"domains": noise_domains, "addresses": noise_addresses},
            "open_loops_raw": loops[:25]}


def group_loops(raw: list[dict[str, Any]], pack: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Raw loops keyed by project slug, using the known-people mapping;
    unknown senders go under 'inbox'."""
    from core.context_pack import sender_hint

    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in raw:
        hint = sender_hint(item.get("addr", ""), pack) or {}
        slug = hint.get("project") or "inbox"
        # "mail" marks a loop read from Gmail (the gate masks it from models in
        # safe-start); "addr" lets an exclusion added later drop it.
        out[slug].append({**{k: item[k] for k in ("owner", "who", "since", "text", "addr") if k in item},
                          "mail": True})
    return dict(out)


# ---------------------------------------------------------------- Gmail I/O


def _when(msg: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(msg.get("internalDate") or 0) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _hdr(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name:
            return h.get("value") or ""
    return ""


async def _list_ids(conn: Any, path: str, key: str, q: str, cap: int) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while len(ids) < cap:
        params: dict[str, Any] = {"q": q, "maxResults": min(100, cap - len(ids))}
        if token:
            params["pageToken"] = token
        page = await conn._get(path, params)
        ids += [x["id"] for x in page.get(key) or []]
        token = page.get("nextPageToken")
        if not token:
            break
    return ids


async def _threads(conn: Any, ids: list[str]) -> list[Thread]:
    sem = asyncio.Semaphore(6)

    async def one(tid: str) -> Thread | None:
        async with sem:
            try:
                data = await conn._get(f"/threads/{tid}", {
                    "format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Subject"]})
            except Exception as exc:  # noqa: BLE001 - one bad thread is skipped
                log.warning("mail learning: thread %s skipped: %s", tid, exc)
                return None
        msgs = [Message(_hdr(m, "from"), _hdr(m, "to"), _hdr(m, "cc"), _hdr(m, "subject"), _when(m))
                for m in data.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
        return Thread(tid, msgs)

    got = await asyncio.gather(*(one(t) for t in ids))
    return [t for t in got if t]


async def _unread_senders(conn: Any, days: int, cap: int) -> list[str]:
    ids = await _list_ids(conn, "/messages", "messages", f"is:unread newer_than:{days}d", cap)
    sem = asyncio.Semaphore(6)

    async def one(mid: str) -> str:
        async with sem:
            try:
                m = await conn._get(f"/messages/{mid}", {"format": "metadata", "metadataHeaders": ["From"]})
            except Exception:  # noqa: BLE001
                return ""
        return _hdr(m, "from")

    return [s for s in await asyncio.gather(*(one(i) for i in ids)) if s]


def gmail_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and settings.google_refresh_token)


async def _me(conn: Any) -> str:
    profile = await conn._get("/profile")
    return (profile.get("emailAddress") or "").lower()


def _read_learned(path: Path | None = None) -> dict[str, Any]:
    path = path or LEARNED_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("learned context unreadable, starting fresh: %s", exc)
        return {}


def _write_learned(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or LEARNED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Learned by Atlas from Gmail metadata (core/mail_learning.py). Regenerated\n"
              "# weekly; open loops refresh daily. Edit config/context.yaml instead: it wins.\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)


async def _label_projects(people: list[dict[str, Any]]) -> None:
    """Fill `project` on learned people the hand-written pack does not know,
    with one Haiku call over their subjects. Best effort; leaves None."""
    from core import llm, privacy
    from core.context_pack import load, sender_hint
    from core.projects import list_projects

    if not privacy.mail_model_reads():
        return  # safe-start: no model reads mail subjects (core/privacy.py)
    pack = load()
    todo = [p for p in people if p["tier"] <= 2 and not sender_hint(p["emails"][0], pack)]
    if not todo:
        return
    try:
        slugs = [p.slug for p in await list_projects(status=None)]
    except Exception:  # noqa: BLE001
        return
    schema = {"type": "object", "additionalProperties": False, "required": ["labels"],
              "properties": {"labels": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["email", "project"],
                  "properties": {"email": {"type": "string"},
                                 "project": {"type": ["string", "null"], "enum": [*slugs, None]}}}}}}
    lines = [f"{p['emails'][0]} ({p['name']}): {' | '.join(p.get('subjects') or [])}" for p in todo[:30]]
    data = await llm.complete_json(
        system="Map each correspondent to the project slug their email subjects belong to, or null. "
               "Projects: " + ", ".join(slugs),
        user="\n".join(lines), schema=schema, tier="scan", max_tokens=2048, purpose="mail_learning")
    for row in (data or {}).get("labels") or []:
        for p in todo:
            if p["emails"][0] == str(row.get("email", "")).lower() and row.get("project"):
                p["project"] = row["project"]


async def learn(conn: Any | None = None, *, days: int = 180, max_threads: int = 400,
                unread_days: int = 30, max_unread: int = 300, label: bool = True) -> dict[str, Any]:
    """The weekly full pass. Returns a summary; writes LEARNED_PATH."""
    from core.connectors.gmail import GmailConnector
    from core.context_pack import load

    conn = conn or GmailConnector()
    me = await _me(conn)
    ids = await _list_ids(conn, "/threads", "threads", f"in:sent newer_than:{days}d", max_threads)
    threads = await _threads(conn, ids)
    unread = await _unread_senders(conn, unread_days, max_unread)
    result = analyze(me, threads, unread)
    if label:
        try:
            await _label_projects(result["people"])
        except Exception:  # noqa: BLE001
            log.exception("mail learning: project labels skipped")
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    learned = {
        "learned_at": now,
        "window_days": days,
        "people": [{k: v for k, v in p.items() if k != "subjects" and v is not None} for p in result["people"]],
        "noise": result["noise"],
        "open_loops_at": now,
        "open_loops": group_loops(result["open_loops_raw"], load()),
    }
    _write_learned(learned)
    summary = {"threads": len(threads), "people": len(learned["people"]),
               "noise_domains": len(result["noise"]["domains"]),
               "open_loops": len(result["open_loops_raw"])}
    log.info("mail learning: %s", summary)
    return summary


async def refresh_open_loops(conn: Any | None = None, *, days: int = 21, max_threads: int = 150) -> dict[str, Any]:
    """The daily pass: recompute open loops only, from recent sent threads
    and recent inbound mail from known people."""
    from core.connectors.gmail import GmailConnector
    from core.context_pack import load

    conn = conn or GmailConnector()
    pack = load()
    me = await _me(conn)
    ids = await _list_ids(conn, "/threads", "threads", f"in:sent newer_than:{days}d", max_threads)
    known = [e for p in pack.get("people") or [] if p.get("tier", 3) <= 2 for e in p.get("emails") or []]
    if known:
        q = f"newer_than:{days}d -in:sent from:({' OR '.join(known[:40])})"
        ids += await _list_ids(conn, "/threads", "threads", q, 60)
    threads = await _threads(conn, list(dict.fromkeys(ids)))
    result = analyze(me, threads, [], loop_window_days=days)
    learned = _read_learned()
    learned["open_loops_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    learned["open_loops"] = group_loops(result["open_loops_raw"], pack)
    _write_learned(learned)
    summary = {"threads": len(threads), "open_loops": len(result["open_loops_raw"])}
    log.info("mail learning (daily loops): %s", summary)
    return summary


def learned_age_days(path: Path | None = None) -> float | None:
    data = _read_learned(path)
    at = data.get("learned_at")
    if not at:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(at))).total_seconds() / 86400
    except ValueError:
        return None


async def scheduled_learn() -> None:
    """Weekly job. Never raises."""
    if not gmail_configured():
        return
    try:
        await learn()
    except Exception:  # noqa: BLE001
        log.exception("weekly mail learning failed")


async def scheduled_refresh() -> None:
    """Daily job: a full learn when nothing has been learned yet, else
    the open-loops refresh. Never raises."""
    if not gmail_configured():
        return
    try:
        age = learned_age_days()
        if age is None or age > 8:
            await learn()
        else:
            await refresh_open_loops()
    except Exception:  # noqa: BLE001
        log.exception("daily mail refresh failed")


__all__ = ["LEARNED_PATH", "Message", "Thread", "analyze", "gmail_configured", "group_loops",
           "learn", "learned_age_days", "refresh_open_loops", "scheduled_learn", "scheduled_refresh"]
