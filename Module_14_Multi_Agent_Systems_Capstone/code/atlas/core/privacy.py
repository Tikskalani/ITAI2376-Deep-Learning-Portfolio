"""The privacy gate: what may reach a model from mail, the calendar and GitHub.

One source of truth for two questions:

  is this item private?   is_private_mail / is_private_event / is_private_repo,
                           and screen() for patient-data patterns. A match
                           carries the rule's LABEL ("dental clients"), never
                           the address or text that matched.
  may a model see it?     model_visible(signal). Model-path code calls only
                           this. A signal is visible when it is not private,
                           and either its source is not mail, calendar or
                           GitHub, or mail model reads are on.

The rules live in data/model_exclude.yaml (gitignored: the list is Trilok's).
config/model_exclude.example.yaml documents every field. A missing file means
no exclusion rules, but safe-start (no model reads of mail, calendar or GitHub)
still holds until both switches are on: ATLAS_MAIL_MODEL_READS=true in .env
and mail_model_reads: true in the rules file. Either one off keeps it off.

Exclusion beats every other rule, the known-people list included. A private
item is stored with no body and the subject "Private message (<label>)"; the
brief reports private mail as counts per label, appended by code.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

import yaml

from core.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

RULES_PATH = REPO_ROOT / "data" / "model_exclude.yaml"
EXAMPLE_PATH = REPO_ROOT / "config" / "model_exclude.example.yaml"
# Mail-like sources: hidden from models until mail model reads are on. Canvas
# (his own coursework) and the rest are not.
GATED_SOURCES = frozenset({"gmail", "gcal", "github"})
SCREEN_LABEL = "possible patient data"
_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class Match:
    """Why an item is private: the rule's label, and which kind of rule."""

    label: str
    kind: str  # sender, domain, label, recipient, calendar, github, screen


# ------------------------------------------------------------------ the rules


@dataclass(frozen=True)
class Rule:
    label: str
    senders: frozenset[str]
    domains: tuple[str, ...]
    labels: frozenset[str]
    recipients_domains: tuple[str, ...]
    attendee_domains: tuple[str, ...]
    title_keywords: tuple[str, ...]
    github_orgs: frozenset[str]
    github_repos: frozenset[str]


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...] = ()
    content_screen: bool = True
    mail_model_reads: bool = False


def _lower(items: Iterable[Any] | None) -> list[str]:
    return [str(x).strip().lower().lstrip("@") for x in items or [] if str(x).strip()]


def parse(data: Mapping[str, Any] | None) -> Rules:
    d = data or {}
    out = []
    for i, r in enumerate(d.get("rules") or []):
        if not isinstance(r, Mapping):
            continue
        cal = r.get("calendar") or {}
        gh = r.get("github") or {}
        out.append(Rule(
            label=str(r.get("label") or f"rule {i + 1}").strip()[:60],
            senders=frozenset(_lower(r.get("senders"))),
            domains=tuple(_lower(r.get("domains"))),
            labels=frozenset(_lower(r.get("labels"))),
            recipients_domains=tuple(_lower(r.get("recipients_domains"))),
            attendee_domains=tuple(_lower(cal.get("attendee_domains"))),
            title_keywords=tuple(_lower(cal.get("title_keywords"))),
            github_orgs=frozenset(_lower(gh.get("orgs"))),
            github_repos=frozenset(_lower(gh.get("repos"))),
        ))
    return Rules(rules=tuple(out),
                 content_screen=d.get("content_screen", True) is not False,
                 mail_model_reads=d.get("mail_model_reads") is True)


_cache: dict[str, Any] = {"at": 0.0, "mtime": None, "rules": Rules()}


def load(path: Path | None = None) -> Rules:
    """The rules, re-read when the file changes. Missing or unreadable: no rules
    (and model reads stay off, since the file's switch is not true)."""
    p = path or RULES_PATH
    if path is None and time.monotonic() - _cache["at"] < _CACHE_SECONDS:
        return _cache["rules"]  # type: ignore[no-any-return]
    try:
        mtime = p.stat().st_mtime
    except OSError:
        rules = Rules()
        if path is None:
            _cache.update(at=time.monotonic(), mtime=None, rules=rules)
        return rules
    if path is None and mtime == _cache["mtime"]:
        _cache["at"] = time.monotonic()
        return _cache["rules"]  # type: ignore[no-any-return]
    try:
        rules = parse(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError) as exc:
        # Fail closed on content: a broken file keeps model reads off.
        log.error("privacy: %s is unreadable (%s); no model reads of mail until it is fixed", p.name, exc)
        rules = Rules(content_screen=True, mail_model_reads=False)
    if path is None:
        _cache.update(at=time.monotonic(), mtime=mtime, rules=rules)
    return rules


def reset_cache() -> None:
    _cache.update(at=0.0, mtime=None, rules=Rules())


def mail_model_reads(rules: Rules | None = None) -> bool:
    """Both switches on: the setting and the rules file."""
    r = rules or load()
    return bool(settings.mail_model_reads) and r.mail_model_reads


# ------------------------------------------------------------------ matching


def _addr(value: str) -> str:
    return (parseaddr(value or "")[1] or value or "").strip().lower()


def _domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def _domain_match(domain: str, suffixes: Iterable[str]) -> bool:
    return bool(domain) and any(domain == s or domain.endswith("." + s) for s in suffixes)


def _addresses(values: Iterable[str]) -> list[str]:
    return [a.lower() for _, a in getaddresses([v for v in values if v]) if a]


def is_private_mail(
    sender: str,
    *,
    recipients: Iterable[str] = (),
    labels: Iterable[str] = (),
    rules: Rules | None = None,
) -> Match | None:
    """The first rule a message matches: sender, sender domain, Gmail label name,
    or a To/Cc domain. Checked before the known-people list, which it beats."""
    r = rules or load()
    addr = _addr(sender)
    dom = _domain_of(addr)
    names = {str(x).strip().lower() for x in labels}
    rcpt_domains = [_domain_of(a) for a in _addresses(recipients)]
    for rule in r.rules:
        if addr and addr in rule.senders:
            return Match(rule.label, "sender")
        if _domain_match(dom, rule.domains):
            return Match(rule.label, "domain")
        if names & rule.labels:
            return Match(rule.label, "label")
        if any(_domain_match(d, rule.recipients_domains) for d in rcpt_domains):
            return Match(rule.label, "recipient")
    return None


def is_private_event(
    *,
    title: str = "",
    attendees: Iterable[str] = (),
    organizer: str = "",
    rules: Rules | None = None,
) -> Match | None:
    """A calendar event: any attendee's or the organizer's domain, a title keyword,
    or a rule's sender/domain lists (a client's meeting is a client's mail)."""
    r = rules or load()
    emails = [_addr(a) for a in [organizer, *attendees] if a]
    domains = [_domain_of(a) for a in emails]
    low_title = (title or "").lower()
    for rule in r.rules:
        if any(_domain_match(d, rule.attendee_domains) or _domain_match(d, rule.domains) for d in domains):
            return Match(rule.label, "calendar")
        if any(a in rule.senders for a in emails):
            return Match(rule.label, "calendar")
        if any(k and k in low_title for k in rule.title_keywords):
            return Match(rule.label, "calendar")
    return None


def is_private_repo(repo: str, *, rules: Rules | None = None) -> Match | None:
    """A GitHub repo ("owner/name") or its owner."""
    r = rules or load()
    full = (repo or "").strip().lower()
    owner = full.split("/", 1)[0]
    for rule in r.rules:
        if full in rule.github_repos or owner in rule.github_orgs:
            return Match(rule.label, "github")
    return None


# ------------------------------------------------------------------ the content screen
#
# Local regexes only, no model. Precision can be low: a hit only keeps the
# message from models; it is still stored (with no body) and shown to him.
_SCREEN = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                    # SSN-shaped
    re.compile(r"\bD\.?O\.?B\.?\b|\bdate\s+of\s+birth\b", re.I),              # date of birth
    re.compile(r"\bMRN\b|\bmedical\s+record\s+(?:number|no\.?|#)?", re.I),    # medical record
    re.compile(r"\b[Pp]atient(?:'s)?(?:\s+name)?\s*[:\-]?\s+[A-Z][a-z]+\s+[A-Z][a-z]+"),  # patient Jane Doe
    re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+,?\s+(?:a|our|the)?\s*patient\b"),          # Jane Doe, a patient
    re.compile(r"\b(?:member|subscriber|policy|insurance|insured)\s*(?:id\b|number\b|no\b\.?|#)", re.I),
)


def screen(text: str, *, rules: Rules | None = None) -> Match | None:
    """Patient-data patterns in a message's own text, when the screen is on."""
    r = rules or load()
    if not r.content_screen or not text:
        return None
    if any(p.search(text) for p in _SCREEN):
        return Match(SCREEN_LABEL, "screen")
    return None


# ------------------------------------------------------------------ the gate


def placeholder(label: str) -> str:
    return f"Private message ({label})"


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def is_private(item: Any) -> bool:
    return bool(_get(item, "private"))


def model_visible(item: Any, *, rules: Rules | None = None) -> bool:
    """The only gate model-path code may call. `item` is a signal row or Signal."""
    if is_private(item):
        return False
    return _get(item, "source") not in GATED_SOURCES or mail_model_reads(rules)


def visible(rows: Iterable[Any]) -> list[Any]:
    """The model-visible ones, in order."""
    return [r for r in rows if model_visible(r)]


def model_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A signal row as a model may see it: unchanged when visible, otherwise only
    its source, time, project and urgency, with a placeholder for a subject."""
    out = dict(row)
    if model_visible(row):
        return out
    label = (placeholder(str(row.get("private_rule") or "private")) if is_private(row)
             else f"{row.get('source')} signal (not read by a model)")
    for key in ("body", "actor", "url", "raw", "deadline"):
        out[key] = None
    out["subject"] = label
    out["triage_notes"] = label
    return out


def brief_line(rows: Iterable[Any]) -> str:
    """What a brief says about signals it may not show a model: counts per rule
    label for private ones, and one count for mail still in safe-start. No
    sender, subject or body, ever. Empty when there is nothing to say."""
    private: dict[str, int] = {}
    unread = 0
    for r in rows:
        if is_private(r):
            label = str(_get(r, "private_rule") or "private")
            private[label] = private.get(label, 0) + 1
        elif not model_visible(r):
            unread += 1
    parts = []
    if private:
        parts.append("Private mail Atlas did not read: "
                     + ", ".join(f"{k} {v}" for k, v in sorted(private.items())) + ".")
    if unread:
        parts.append(f"{unread} new mail, calendar or GitHub signal{'s' if unread != 1 else ''}, "
                     "not yet read by a model (safe-start).")
    return " ".join(parts)


__all__ = [
    "EXAMPLE_PATH",
    "GATED_SOURCES",
    "Match",
    "RULES_PATH",
    "Rules",
    "SCREEN_LABEL",
    "brief_line",
    "is_private",
    "is_private_event",
    "is_private_mail",
    "is_private_repo",
    "load",
    "mail_model_reads",
    "model_view",
    "model_visible",
    "parse",
    "placeholder",
    "reset_cache",
    "screen",
    "visible",
]
