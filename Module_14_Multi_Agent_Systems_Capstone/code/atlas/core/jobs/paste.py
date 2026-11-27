"""Paste a job link: Atlas finds the posting, tracks it, drafts, and applies.

Trilok, 2026-10-23: "there should be a way for me to paste links and for it
to apply, like linkedin links and things like that and track everything".

What a link becomes:
  * A Greenhouse, Lever, Ashby or Workable posting: read from the board's
    own API, queued (outside the daily cap: he chose it), drafted by the
    career agent, then applied by the submitter like any approved item. The
    pre-submit check, the trust ladder and dry run all still apply.
  * A company careers page: read once through the guarded fetcher
    (core/web: public addresses only); an embedded or linked board form on
    it is followed as above.
  * LinkedIn: the public job page is read once. If the job's apply button
    leads to the company's own board, Atlas applies there. Atlas never
    drives LinkedIn's own Easy Apply: LinkedIn forbids automated applying,
    and Atlas does not apply on boards that forbid bots (docs/atlas-vision).
    The role is tracked and drafted; he applies with one click on LinkedIn.
  * Anything else: tracked and drafted, with the link to apply by hand.

Every pasted link lands in the Career pipeline with its history.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from core import db
from core.jobs import config as jobs_config
from core.jobs import store, untrusted

log = logging.getLogger(__name__)

SOURCE = "pasted"
_UUID = r"[0-9a-fA-F-]{36}"
# Board links, wherever they appear: the pasted URL itself, a careers page's
# embed, or LinkedIn's "apply on company website" redirect.
_BOARD_LINKS = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed/)([\w-]+)/jobs/(\d+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?(?:[^\"'\s]*&)?for=([\w-]+)(?:&|&amp;)token=(\d+)")),
    ("lever", re.compile(rf"jobs\.lever\.co/([\w.-]+)/({_UUID})")),
    ("ashby", re.compile(rf"jobs\.ashbyhq\.com/([\w.%-]+)/({_UUID})")),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)/j/([0-9A-Za-z]+)")),
]
_GH_FOR = re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)")
_LINKEDIN_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[\w-]*?-)?(\d{6,})")


class PasteError(ValueError):
    """The link cannot be used; the message says why, in plain words."""


@dataclass
class Found:
    """A posting found from a link, before it is stored."""
    posting: dict[str, Any]
    via: str                       # "board", "careers page", "linkedin", "page"
    flags: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ reading links


def board_link(text: str) -> tuple[str, str, str] | None:
    """(source, board, external id) of the first board posting linked in text,
    or None. A careers page that embeds Greenhouse by gh_jid is found too."""
    text = text or ""
    for source, rx in _BOARD_LINKS:
        m = rx.search(text)
        if m:
            return source, unquote(m.group(1)), m.group(2)
    jid = re.search(r"[?&]gh_jid=(\d+)", text)
    board = _GH_FOR.search(text)
    if jid and board:
        return "greenhouse", board.group(1), jid.group(1)
    return None


def linkedin_job_id(url: str) -> str | None:
    m = _LINKEDIN_JOB.search(url or "")
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url or "").query)
    if "linkedin.com" in (urlparse(url or "").hostname or "") and q.get("currentJobId"):
        cid = q["currentJobId"][0]
        return cid if cid.isdigit() else None
    return None


def linkedin_apply_url(html: str) -> str | None:
    """The company's own apply link on a LinkedIn job page, when the job is
    applied for off LinkedIn ("Apply on company website")."""
    m = re.search(r'externalApply[^"\'<>\s]*?[?&](?:amp;)?url=(https?%3A[^&"\'<>\s]+)', html or "")
    return unquote(htmllib.unescape(m.group(1))) if m else None


def job_ld(html: str) -> dict[str, Any] | None:
    """The schema.org JobPosting a page carries, if any (most boards and
    LinkedIn's public job pages do)."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", flags=re.S | re.I):
        try:
            data = json.loads(htmllib.unescape(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.S | re.I)
    return _strip(m.group(1))[:200] if m else ""


def _ld_posting(ld: dict[str, Any], url: str) -> dict[str, Any]:
    org = ld.get("hiringOrganization") or {}
    place = ld.get("jobLocation") or {}
    place = place[0] if isinstance(place, list) and place else place
    addr = (place.get("address") or {}) if isinstance(place, dict) else {}
    loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry"))
                    if isinstance(x, str) and x)
    remote = str(ld.get("jobLocationType") or "").upper() == "TELECOMMUTE" or None
    return {"company": _strip(str(org.get("name") or "") if isinstance(org, dict) else str(org)),
            "title": _strip(ld.get("title") or ""), "location": loc, "remote": remote,
            "description": _strip(ld.get("description") or "")[:8000], "url": url}


def _manual(url: str, data: dict[str, Any]) -> dict[str, Any]:
    host = (urlparse(url).hostname or "link").removeprefix("www.")
    return {"source": "manual", "board": host, "external_id": hashlib.sha256(url.encode()).hexdigest()[:16],
            "company": data.get("company") or "", "title": data.get("title") or "", "location": data.get("location"),
            "remote": data.get("remote"), "url": url, "description": data.get("description")}


def _greenhouse_boards_for(url: str) -> list[str]:
    """Greenhouse boards a careers site may embed: configured companies whose
    board or name is in the host, then the host's own name."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return []
    label = host.split(".")[-2] if host.count(".") >= 1 else host
    out = [str(c["board"]) for c in jobs_config.companies() if c.get("source") == "greenhouse"
           and (str(c["board"]).lower() in host or str(c.get("name", "")).lower().replace(" ", "") in host)]
    if re.fullmatch(r"[a-z0-9-]{2,40}", label) and label not in out:
        out.append(label)
    return out


def _company_name(source: str, board: str) -> str:
    for c in jobs_config.companies():
        if c.get("source") == source and c.get("board") == board:
            return str(c.get("name"))
    return board.replace("-", " ").replace("_", " ").title()


# ------------------------------------------------------------------ the network


async def fetch_board_posting(source: str, board: str, ext: str) -> dict[str, Any] | None:
    """One posting from its board's API, in the shape discovery stores, or
    None when the board does not list it (closed, or a wrong link)."""
    from core.jobs import discovery

    name = _company_name(source, board)
    async with httpx.AsyncClient(timeout=discovery._TIMEOUT, headers=discovery._HEADERS, follow_redirects=True) as c:
        if source == "greenhouse":
            r = await c.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{ext}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            j = r.json()
            loc = (j.get("location") or {}).get("name") or ""
            desc = discovery._strip_html(htmllib.unescape(j.get("content") or ""))[:8000]
            lo, hi = discovery.salary_from_text(desc)
            return {"source": "greenhouse", "board": board, "external_id": str(j["id"]),
                    "company": j.get("company_name") or name, "title": j.get("title") or "", "location": loc,
                    "remote": "remote" in loc.lower(), "url": j.get("absolute_url"),
                    "posted_at": discovery._ts(j.get("first_published") or j.get("updated_at")),
                    "description": desc, "salary_min": lo, "salary_max": hi}
        posts = await discovery.FETCHERS[source](c, {"board": board, "name": name})
    return next((p for p in posts if str(p["external_id"]).lower() == ext.lower()), None)


async def _page(url: str) -> tuple[str, str]:
    from core.web import FetchRefused, fetch_html

    try:
        return await fetch_html(url)
    except FetchRefused as exc:
        raise PasteError(f"Atlas will not open that link: {exc}") from exc
    except httpx.HTTPError as exc:
        raise PasteError(f"the page did not open ({type(exc).__name__}); paste the posting's own link instead") from exc


async def find(url: str) -> Found:
    """What a pasted link points at. Network only through the board APIs
    and the guarded page fetch. Raises PasteError."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url) or len(url) > 2000:
        raise PasteError("paste a full link starting with https://")
    direct = board_link(url)
    if direct:
        p = await fetch_board_posting(*direct)
        if p is None:
            raise PasteError("the board does not list that job any more; it may have closed")
        return Found(p, "board")

    if linkedin_job_id(url) is not None:
        jid = linkedin_job_id(url)
        final, html = await _page(f"https://www.linkedin.com/jobs/view/{jid}")
        apply_at = linkedin_apply_url(html)
        target = board_link(apply_at or "")
        if target:
            p = await fetch_board_posting(*target)
            if p is not None:
                return Found(p, "linkedin")
        ld = job_ld(html)
        data = _ld_posting(ld, final) if ld else {"title": _title_of(html).split(" | ")[0]}
        posting = _manual(f"https://www.linkedin.com/jobs/view/{jid}", data)
        note = ("LinkedIn: Atlas never automates applying on LinkedIn (its terms forbid it). "
                "The materials are ready; apply there with one click.")
        if apply_at and not target:
            posting["url"] = apply_at
            note = "Applied for on the company's own site, which Atlas cannot fill; the materials are ready."
        return Found(posting, "linkedin", [note])

    final, html = await _page(url)
    # The page's own URL first (its gh_jid can pair with the embed's board in the html).
    target = board_link(final) or board_link(url + "\n" + final + "\n" + html) or board_link(html)
    if target:
        p = await fetch_board_posting(*target)
        if p is not None:
            return Found(p, "careers page")
    # A careers page that embeds Greenhouse by script: the job number is in the
    # pasted link (gh_jid), the board is usually the site's own name
    # (databricks.com -> databricks, 2026-10-23). The redirect may drop gh_jid.
    gh = re.search(r"[?&]gh_jid=(\d+)", url) or re.search(r"[?&]gh_jid=(\d+)", final)
    if gh:
        for board in _greenhouse_boards_for(final) + _greenhouse_boards_for(url):
            p = await fetch_board_posting("greenhouse", board, gh.group(1))
            if p is not None:
                return Found(p, "careers page")
    ld = job_ld(html)
    data = _ld_posting(ld, final) if ld else {"title": _title_of(html)}
    return Found(_manual(final, data), "page", ["Atlas does not fill this site's form; the materials are ready."])


# ------------------------------------------------------------------ storing it


async def take(url: str, *, company: str = "", title: str = "") -> dict[str, Any]:
    """Find the posting, store it, track it and queue it for drafting.
    company and title fill in what a page did not say. Returns the queue
    item with what happened; a link pasted twice returns the first one."""
    found = await find(url)
    p = found.posting
    p["company"] = (p.get("company") or company or "").strip()
    p["title"] = (p.get("title") or title or "").strip()
    if not p["company"] or not p["title"]:
        raise PasteError("Atlas could not read the company and role from that page; add them and paste again")
    posting_id, _new = await store.upsert_posting(p)
    existing = await db.fetchrow("select id, status, application_id from job_queue where posting_id = $1", posting_id)
    if existing:
        return {"queue_id": existing["id"], "status": existing["status"], "application_id": existing["application_id"],
                "company": p["company"], "title": p["title"], "via": found.via, "duplicate": True,
                "applies": p["source"] != "manual"}
    flags = found.flags + untrusted.clean(p.get("description")).flags
    app = await store.upsert_application(p["company"], p["title"], status="interested", source=SOURCE,
                                         url=p.get("url") or url, posting_id=posting_id)
    await store.record_event(app["id"], "pasted", f"You pasted {url[:300]} ({found.via})")
    row = await db.fetchrow(
        "insert into job_queue (posting_id, application_id, flags) values ($1, $2, $3) "
        "on conflict (posting_id) do nothing returning id, status",
        posting_id, app["id"], flags,
    )
    if row is None:  # the daily build queued it in the same instant
        row = await db.fetchrow("select id, status from job_queue where posting_id = $1", posting_id)
    await store.set_posting_status(posting_id, "shown")
    return {"queue_id": row["id"], "status": row["status"], "application_id": app["id"], "posting_id": posting_id,
            "company": p["company"], "title": p["title"], "via": found.via, "duplicate": False,
            "applies": p["source"] != "manual", "flags": flags}


async def draft_and_apply(item: dict[str, Any], *, apply: bool) -> str:
    """Draft the materials, then, when he asked and the board is one Atlas
    fills, approve it and run the submitter on it. Returns the final status."""
    from core.jobs import queue, submitter

    status = await queue.draft({"id": item["queue_id"], "posting_id": item["posting_id"],
                                "application_id": item["application_id"]})
    if status != "ready" or not apply or not item.get("applies"):
        return status
    if await queue.decide(item["queue_id"], "apply") is None:
        return status
    await store.record_event(item["application_id"], "approved", "Approved when you pasted the link")
    out = await submitter.submit_one(item["queue_id"])
    return str(out.get("status") or "approved")


__all__ = ["Found", "PasteError", "board_link", "draft_and_apply", "fetch_board_posting", "find", "job_ld",
           "linkedin_apply_url", "linkedin_job_id", "take"]
