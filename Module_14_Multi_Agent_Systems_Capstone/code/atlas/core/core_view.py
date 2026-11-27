"""The Today room (Neural Core): agents, hand-offs, decisions and health.

Everything here is read from what Atlas already records: the `events` table
(dispatch, dispatch.done, run.*, approval.*), `approvals`, `night_tasks`,
spend, `slo_checks` and the night-run heartbeat. Nothing is invented: with
no events there are no messages, and the network only idles.

Every line shown is a template over event metadata (no model call). Free
text an event carries (a task, a run summary) is shown only when the
content screen passes and no exclusion rule's domain, sender, label or
keyword appears in it; otherwise the line is "Private item" with the agent
names only. Approval previews never carry an email body, and a subject or
recipient only when mail is model-visible (docs/privacy.md).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml

from core import db, privacy
from core.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

CONFIG_PATH = REPO_ROOT / "config" / "core_view.yaml"
PRIVATE = "Private item"
YOU, ATLAS = "YOU", "ATLAS"
# The kinds that become a message on the map and in the feed.
MESSAGE_KINDS = ("dispatch", "dispatch.done", "approval.asked", "approval.decided",
                 "approval.finished", "approval.expired")
STATE_KINDS = ("run.started", "run.tool", "run.finished")
FEED_SIZE = 7
NIGHT_PREFIX = "night:"

_TOOL_LABELS = {
    "draft_email": "an email draft",
    "calendar_create_event": "a calendar event",
}


class DecisionError(Exception):
    """A decision could not be made; `status` is the HTTP code, the message is plain words."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ roster


def _registered() -> list[str]:
    from core.runtime.dispatcher import registered_agents

    return registered_agents()


def roster() -> list[dict[str, str]]:
    """The ring, clockwise from the top: config order, registered agents only,
    then any registered agent the config does not name."""
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("could not read %s; the ring falls back to the registry order", CONFIG_PATH)
        data = {}
    known = set(_registered())
    out: list[dict[str, str]] = []
    for item in data.get("ring") or []:
        aid = str((item or {}).get("id") or "")
        if aid in known and aid not in {a["id"] for a in out}:
            out.append({"id": aid, "name": str(item.get("name") or aid)})
    for aid in sorted(known - {a["id"] for a in out}):
        out.append({"id": aid, "name": aid.replace("_", " ").capitalize()})
    return out


def _names() -> dict[str, str]:
    return {a["id"]: a["name"] for a in roster()}


def endpoint(who: str | None, names: dict[str, str]) -> str:
    """An agent's display name, YOU for Trilok's own surfaces, ATLAS for its loops."""
    w = (who or "").strip()
    if w in names:
        return names[w]
    if w in ("owner", "") or w.startswith(("surface/", "web/", "owner/")):
        return YOU
    return ATLAS


# ------------------------------------------------------------------ privacy


def _rule_terms(rules: privacy.Rules) -> list[str]:
    terms: list[str] = []
    for r in rules.rules:
        terms += [*r.senders, *r.domains, *r.labels, *r.recipients_domains,
                  *r.attendee_domains, *r.title_keywords, *r.github_orgs, *r.github_repos]
    return [t for t in terms if t]


def safe_text(text: Any, limit: int = 140) -> str | None:
    """Free text an event carries, cut to one short line, or None when it may
    not be shown: the content screen hits, or an exclusion rule's term appears."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return None
    rules = privacy.load()
    if privacy.screen(s, rules=rules) is not None:
        return None
    low = s.lower()
    if any(t in low for t in _rule_terms(rules)):
        return None
    s = s.replace("—", ", ").replace("–", "-")  # no em-dashes in what Trilok reads
    first = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
    if len(first) > limit:
        first = first[: limit - 1].rstrip(" ,.;:") + "…"
    return first


def tool_label(tool: str | None) -> str:
    t = str(tool or "a tool")
    return _TOOL_LABELS.get(t, t.replace("_", " "))


# ------------------------------------------------------------------ time


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def local_iso(dt: datetime | None) -> str | None:
    """ISO 8601 in Houston time (settings.timezone), as the briefs use."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_tz()).isoformat(timespec="seconds")


def _parse_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ events to view


def _message(ev: dict[str, Any], frm: str, to: str, summary: str, kind: str = "") -> dict[str, Any]:
    return {"type": "message", "id": ev.get("id"), "ts": local_iso(_parse_at(ev.get("at"))),
            "from": frm, "to": to, "summary": summary, "kind": kind}


async def _approval_private(approval_id: Any) -> bool:
    """True when the approval's own input touches an excluded address, event
    or patient data. Unknown or unreadable counts as private."""
    try:
        row = await db.fetchrow("select action_payload from approvals where id = $1",
                                UUID(str(approval_id)))
    except (ValueError, TypeError):
        return True
    if row is None:
        return True
    return _input_private(dict(row["action_payload"] or {}))


def _input_private(payload: dict[str, Any]) -> bool:
    inp = payload.get("input") or {}
    if not isinstance(inp, dict):
        return True
    rules = privacy.load()
    people = [str(inp.get(k) or "") for k in ("to", "cc", "bcc")]
    people += [str(x) for x in inp.get("attendees") or []]
    if any(people) and privacy.is_private_mail("", recipients=people, rules=rules):
        return True
    title = str(inp.get("summary") or inp.get("title") or "")
    if (title or people) and privacy.is_private_event(title=title, attendees=people, rules=rules):
        return True
    text = "\n".join(str(inp.get(k) or "") for k in ("subject", "body", "summary", "title",
                                                     "description"))
    if privacy.screen(text, rules=rules) is not None:
        return True
    low = text.lower()
    return any(t in low for t in _rule_terms(rules))


async def to_view(ev: dict[str, Any], names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One stored event as the Today room's events: messages, agent states and
    decision changes. Kinds the room does not draw give an empty list."""
    names = names if names is not None else _names()
    kind = ev.get("kind") or ""
    data = ev.get("data") or {}
    agent = ev.get("agent")
    out: list[dict[str, Any]] = []
    if kind == "dispatch":
        target = data.get("target") or _target_from_title(ev.get("title"))
        frm, to = endpoint(data.get("from") or agent, names), endpoint(target, names)
        if frm in (YOU, ATLAS) and to in (YOU, ATLAS):
            return []
        if frm == ATLAS:
            # Atlas's own loops send a whole prompt as the task; the feed names the purpose instead.
            text = f"ATLAS asked {to} {_loop_purpose(data.get('from') or agent)}."
        else:
            task = safe_text(ev.get("detail"))
            text = f"{frm} asked {to}: {task}" if task else PRIVATE if ev.get("detail") else \
                f"{frm} asked {to} to take a task."
        msg = _message(ev, frm, to, text)
        if data.get("thread"):
            msg["href"] = f"#thread/{data['thread']}"  # the Chat conversation it came from
        out.append(msg)
        if target in names:
            out.append({"type": "agent_state", "agent": target, "state": "receiving",
                        "last_action": text})
    elif kind == "dispatch.done":
        frm, to = endpoint(data.get("target") or agent, names), endpoint(data.get("from"), names)
        if frm in (YOU, ATLAS):
            return []
        ok = bool(data.get("ok"))
        text = f"{frm} finished the task for {to}." if ok else f"{frm} could not finish the task for {to}."
        out.append(_message(ev, frm, to, text, "ok" if ok else ""))
    elif kind.startswith("approval."):
        aid = data.get("approval_id")
        name = endpoint(agent, names)
        private = await _approval_private(aid)
        what = PRIVATE.lower() if private else tool_label(_title_tool(ev.get("title")))
        if kind == "approval.asked":
            out.append(_message(ev, name, YOU, f"{name} needs your yes for {what}.", "auth"))
            out.append({"type": "decision_changed", "id": str(aid), "status": "pending"})
            if agent in names:
                out.append({"type": "agent_state", "agent": agent, "state": "waiting",
                            "last_action": f"Waiting on your yes for {what}."})
        elif kind == "approval.decided":
            yes = data.get("decision") == "approved"
            out.append(_message(ev, YOU, name, f"You {'approved' if yes else 'declined'} {what}.",
                                "ok" if yes else ""))
            out.append({"type": "decision_changed", "id": str(aid),
                        "status": "approved" if yes else "declined"})
        elif kind == "approval.finished":
            ok = bool(data.get("ok"))
            out.append(_message(ev, name, YOU, f"{name} {'ran' if ok else 'could not run'} {what}.",
                                "ok" if ok else ""))
            if ok:
                out.append({"type": "decision_changed", "id": str(aid), "status": "sent"})
        elif kind == "approval.expired":
            out.append({"type": "decision_changed", "id": str(aid), "status": "expired"})
    elif kind in STATE_KINDS and agent in names:
        if kind == "run.finished":
            failed = str(data.get("status") or "") in ("failed", "error")
            title = ev.get("title")
            line = (safe_text(title) or PRIVATE) if title else "Finished a run."
            out.append({"type": "agent_state", "agent": agent, "state": "error" if failed else "idle",
                        "last_action": line})
        else:
            out.append({"type": "agent_state", "agent": agent, "state": "active",
                        "last_action": "Working on a run."})
    return out


_LOOP_PURPOSE: tuple[tuple[str, Any], ...] = (
    ("loop/brief/", lambda w: f"for the {w.rsplit('/', 1)[-1].replace('_', ' ')} brief"),
    ("loop/executive", lambda w: "for the half-hourly review"),
    ("loop/senses", lambda w: "to prepare for something that arrived"),
    ("night", lambda w: "for a night-shift task"),
    ("jobs/", lambda w: "for job-search work"),
    ("recordings", lambda w: "to read a recording"),
    ("workflow/", lambda w: f"for the {w.split('/', 1)[-1].replace('_', ' ')} routine"),
)


def _loop_purpose(who: Any) -> str:
    w = str(who or "")
    for prefix, phrase in _LOOP_PURPOSE:
        if w.startswith(prefix):
            return str(phrase(w))
    return "to take a task"


def _target_from_title(title: Any) -> str | None:
    m = re.match(r"asked (\S+)", str(title or ""))
    return m.group(1) if m else None


def _title_tool(title: Any) -> str | None:
    """approval titles read "<agent> wants to run <tool>"."""
    m = re.search(r"run (\S+)$", str(title or ""))
    return m.group(1) if m else None


# ------------------------------------------------------------------ decisions


def _mail_visible() -> bool:
    return privacy.mail_model_reads()


def decision_card(row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """A pending approval as a card: a privacy-gated title and preview, never a body."""
    payload = dict(row.get("action_payload") or {})
    tool = str(payload.get("tool") or "")
    raw = payload.get("input")
    inp: dict[str, Any] = raw if isinstance(raw, dict) else {}
    agent = endpoint(row.get("agent_name"), names)
    base = {"id": str(row["id"]), "kind": tool or "approval", "agent": agent,
            "expires_at": local_iso(row.get("expires_at")), "cost_estimate_usd": None}
    if _input_private(payload):
        return {**base, "title": PRIVATE, "preview": f"{agent} is waiting on your yes."}
    visible = _mail_visible()
    if tool == "draft_email":
        name, addr = parseaddr(str(inp.get("to") or ""))
        who = (name or addr.rsplit("@", 1)[-1]) if visible else ""
        title = f"Approve the email draft to {who}" if who else "Approve an email draft"
        subject = safe_text(inp.get("subject"), 200) if visible else None
        preview = (f"Subject: {subject}" if subject else
                   "The subject stays hidden while mail is private (safe-start).")
        return {**base, "title": title, "preview": preview}
    if tool == "calendar_create_event":
        what = safe_text(inp.get("summary"), 80) if visible else None
        return {**base, "title": f"Add {what} to the calendar" if what else "Add an event to the calendar",
                "preview": f"{agent} drafted it. Nothing is booked until you say yes."}
    return {**base, "title": f"{agent} wants to run {tool_label(tool)}",
            "preview": f"{agent} stopped before acting. Nothing runs until you say yes."}


async def _night_card(names: dict[str, str]) -> dict[str, Any] | None:
    from core.night import night_of

    night = night_of()
    rows = await db.fetch("select agent, budget_usd from night_tasks where night = $1 and status = 'proposed'",
                          night)
    if not rows:
        return None
    budget = [Decimal(str(r["budget_usd"])) for r in rows if r["budget_usd"] is not None]
    agents = sorted({endpoint(r["agent"], names) for r in rows})
    run_at = datetime.combine(night, datetime.min.time(), tzinfo=_tz()).replace(
        hour=settings.night_run_hour, minute=settings.night_run_minute)
    n = len(rows)
    return {"id": f"{NIGHT_PREFIX}{night.isoformat()}", "kind": "night_plan", "agent": ATLAS,
            "title": "Tonight's night-shift plan",
            "preview": f"{n} task{'s' if n != 1 else ''} for {', '.join(agents)}. "
                       f"Runs at {run_at.strftime('%H:%M')} under the night cap.",
            "expires_at": run_at.isoformat(timespec="seconds"),
            "cost_estimate_usd": float(sum(budget)) if budget else None}


async def decisions(names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    names = names if names is not None else _names()
    rows = await db.fetch(
        "select id, agent_name, action_payload, expires_at from approvals "
        "where status = 'pending' and (expires_at is null or expires_at > now()) order by created_at")
    out = [decision_card(dict(r), names) for r in rows]
    night = await _night_card(names)
    if night:
        out.append(night)
    return out


async def decide(decision_id: str, decision: str) -> dict[str, Any]:
    """Approve or decline from the Today room. Deciding the same way twice is a
    success (the hold can fire twice on a slow network); the opposite way, or
    an expired card, is refused in plain words. Never approves on its own."""
    if decision not in ("approve", "decline"):
        raise DecisionError(400, "The decision must be approve or decline.")
    if decision_id.startswith(NIGHT_PREFIX):
        return await _decide_night(decision_id, decision)
    try:
        aid = UUID(decision_id)
    except ValueError as exc:
        raise DecisionError(404, "There is no such decision.") from exc
    from core.runtime import approval

    want = "approved" if decision == "approve" else "rejected"
    row = await approval.check_approval(aid)
    if row is None:
        raise DecisionError(404, "There is no such decision.")
    status = row["status"]
    if status == "pending":
        exp = row.get("expires_at")
        if exp is not None and exp <= datetime.now(UTC):
            await approval.expire_overdue_approvals()
            raise DecisionError(410, f"This expired at {_hhmm(exp)}. Ask the agent again if it still matters.")
        done = await approval.decide_approval(aid, want, decided_by="owner/today")
        if done is None:
            row = await approval.check_approval(aid) or {"status": "unknown"}
            status = row["status"]
        else:
            return {"id": decision_id, "status": _view_status(want), "repeat": False}
    if status == want:
        return {"id": decision_id, "status": _view_status(want), "repeat": True}
    if status == "expired":
        raise DecisionError(410, "This expired before you decided. Ask the agent again if it still matters.")
    raise DecisionError(409, f"This was already {_view_status(status)}.")


def _view_status(status: str) -> str:
    return {"rejected": "declined"}.get(status, status)


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%H:%M")


async def _decide_night(decision_id: str, decision: str) -> dict[str, Any]:
    from core import night

    if decision_id != f"{NIGHT_PREFIX}{night.night_of().isoformat()}":
        raise DecisionError(410, "That night's plan is over.")
    if decision == "approve":
        n = await night.approve_all()
    else:
        ids = await db.fetch("select id from night_tasks where night = $1 and status = 'proposed'",
                             night.night_of())
        n = 0
        for r in ids:
            n += 1 if await night.decide(r["id"], "skip") else 0
    status = "approved" if decision == "approve" else "declined"
    return {"id": decision_id, "status": status, "repeat": n == 0}


# ------------------------------------------------------------------ agents and health


async def agents(names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Each agent's state from the last day of events and its pending approvals."""
    names = names if names is not None else _names()
    rows = await db.fetch(
        "select distinct on (agent) id, at, kind, agent, title, detail, data from events "
        "where agent = any($1::text[]) and kind = any($2::text[]) and at > now() - interval '1 day' "
        "order by agent, at desc",
        list(names), list(STATE_KINDS),
    )
    latest = {r["agent"]: dict(r) for r in rows}
    waiting = {r["agent_name"] for r in await db.fetch(
        "select distinct agent_name from approvals where status = 'pending' "
        "and (expires_at is null or expires_at > now())")}
    out = []
    for aid, name in names.items():
        state, line, at = "idle", "Nothing in the last day.", None
        ev = latest.get(aid)
        if ev:
            view = [v for v in await to_view(ev, names) if v["type"] == "agent_state"]
            if view:
                state, line = view[0]["state"], view[0]["last_action"]
            if state == "active" and ev["at"] < datetime.now(UTC) - timedelta(minutes=30):
                state = "idle"
            at = local_iso(ev["at"])
        if aid in waiting:
            state, line = "waiting", "Waiting on your yes."
        out.append({"id": aid, "name": name, "state": state, "last_action": line[:140], "last_at": at})
    return out


async def recent(names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The last few real messages for the feed, newest first. No packets fly for these."""
    names = names if names is not None else _names()
    rows = await db.fetch(
        "select id, at, kind, agent, title, detail, data from events where kind = any($1::text[]) "
        "and at > now() - interval '1 day' order by id desc limit 40",
        list(MESSAGE_KINDS),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out += [v for v in await to_view(dict(r), names) if v["type"] == "message"]
        if len(out) >= FEED_SIZE:
            break
    return out[:FEED_SIZE]


async def _health_status() -> dict[str, Any]:
    """/api/health's own verdict: {status, problems, warnings}. Unreadable is degraded."""
    try:
        import json

        from core.web.api import health

        resp = await health()
        body = json.loads(bytes(resp.body))
        return {"status": str(body.get("status") or "degraded"),
                "problems": [str(p) for p in body.get("problems") or []],
                "warnings": [str(w) for w in body.get("warnings") or []]}
    except Exception:  # noqa: BLE001 - a status we cannot read is not "normal"
        log.exception("core view: health status unavailable")
        return {"status": "degraded", "problems": ["Atlas could not read its own health."], "warnings": []}


# How far back an unread high-priority alert still makes the day "not normal".
ALERT_HOURS = 24


async def _alerts() -> list[dict[str, Any]]:
    """Unread high-priority notifications from the last day: the loop stalls,
    the database outages. Grouped by title so four stalls read as one line."""
    rows = await db.fetch(
        "select title, count(*) as n, max(created_at) as last from notifications "
        "where priority = 'high' and read_at is null and created_at > now() - make_interval(hours => $1) "
        "group by title order by max(created_at) desc",
        ALERT_HOURS,
    )
    return [{"title": str(r["title"]), "count": int(r["n"]), "last": local_iso(r["last"])} for r in rows]


async def health() -> dict[str, Any]:
    """The day's verdict. It is "normal" only when /api/health is ok, it has no
    warnings, and no high-priority alert from the last day is still unread;
    otherwise "degraded" (or "down"), with the reasons in plain words."""
    from core import costs, night

    h = await _health_status()
    alerts = await _alerts()
    status = {"ok": "normal"}.get(h["status"], h["status"])
    reasons = h["problems"] + h["warnings"] + [
        a["title"] + (f" ({a['count']} times)" if a["count"] > 1 else "") for a in alerts]
    if status == "normal" and reasons:
        status = "degraded"
    today = await costs.daily_spend()
    tonight = night.night_of()
    night_spend = await db.fetchval(
        "select coalesce(sum(cost_usd), 0) from night_tasks where night = $1", tonight)
    briefs = await db.fetch(
        "select day, bool_and(ok) as ok from slo_checks where objective like 'brief/%' "
        "and day > current_date - 7 group by day order by day")
    return {"status": status, "reasons": [safe_text(r, 160) or PRIVATE for r in reasons][:6],
            "spend_today": round(float(today["total"]), 2), "spend_cap": float(settings.daily_usd_cap),
            "night_spend": float(night_spend or 0), "night_cap": float(settings.night_usd_cap),
            "briefs_on_time_7d": [bool(r["ok"]) for r in briefs]}


async def attention(decision_count: int) -> list[dict[str, Any]]:
    """Everything that needs Trilok besides the decision cards, one line each
    with a count and where to go. Each source is read on its own: one that
    fails is left out and logged, never shown as zero."""
    out: list[dict[str, Any]] = []
    if decision_count:
        out.append({"id": "decisions", "label": "Decisions waiting", "count": decision_count,
                    "href": "#inbox/approvals"})

    async def add(item_id: str, label: str, href: str, count_fn: Any) -> None:
        try:
            n = int(await count_fn())
        except Exception:  # noqa: BLE001 - one bad source must not hide the others
            log.exception("core view: attention source %s failed", item_id)
            return
        if n:
            out.append({"id": item_id, "label": label, "count": n, "href": href})

    async def overdue() -> int:
        from core import planner

        return int((await planner.agenda(days=1))["counts"]["overdue"])

    async def alerts() -> int:
        return sum(a["count"] for a in await _alerts())

    async def ideas() -> int:
        from core.discovery import list_review_queue

        return len(await list_review_queue(limit=50))

    async def setup() -> int:
        from core import setup_checklist

        c = await setup_checklist.checklist()
        return int(c["total"]) - int(c["done"])

    await add("overdue", "Overdue tasks", "#plan/week", overdue)
    await add("alerts", "Health alerts unread", "#inbox/notifications", alerts)
    await add("ideas", "Skills to review", "#inbox/skills", ideas)
    await add("setup", "Setup steps left", "#settings/connections", setup)
    return out


async def overnight() -> dict[str, Any]:
    """Last night's run from its heartbeat; zeros when it did not run."""
    from core import night

    row = await db.fetchrow("select metadata from system_heartbeats where kind = 'night/run'")
    meta = dict(row["metadata"] or {}) if row else {}
    last = (night.night_of() - timedelta(days=0 if night.phase() == "morning" else 1)).isoformat()
    if meta.get("night") != last:
        return {"done": 0, "spend": 0.0}
    return {"done": int(meta.get("done") or 0), "spend": float(meta.get("spent") or 0)}


async def state() -> dict[str, Any]:
    names = _names()
    cards = await decisions(names)
    return {"demo": False, "roster": roster(), "agents": await agents(names),
            "decisions": cards, "attention": await attention(len(cards)), "health": await health(),
            "overnight": await overnight(), "recent": await recent(names),
            "now": local_iso(datetime.now(UTC)), "tz": settings.timezone}


def demo_state() -> dict[str, Any]:
    """Demo mode: the page plays its own scripted feed. Nothing here reads the database."""
    return {"demo": True, "roster": roster(), "agents": [], "decisions": [], "attention": [], "health": None,
            "overnight": None, "recent": [], "now": local_iso(datetime.now(UTC)),
            "tz": settings.timezone}


__all__ = ["DecisionError", "PRIVATE", "agents", "attention", "decide", "decision_card", "decisions",
           "demo_state", "endpoint", "health", "overnight", "recent", "roster", "safe_text", "state",
           "to_view", "tool_label"]
