"""Paste a job link (core/jobs/paste.py): found, tracked, drafted, applied.

No network: the board API and the page fetch are faked. Shadowed tables only.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio

from core import db
from core.jobs import paste, queue, submitter
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")

POSTING = {"source": "greenhouse", "board": "acme", "external_id": "4012", "company": "Acme AI",
           "title": "Forward Deployed Engineer", "location": "Remote - US", "remote": True,
           "url": "https://job-boards.greenhouse.io/acme/jobs/4012", "description": "Build agents with customers."}

LINKEDIN_OFFSITE = """<html><head><title>Acme AI hiring Forward Deployed Engineer | LinkedIn</title></head>
<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/externalApply/4012345678?url=https%3A%2F%2Fjob-boards%2Egreenhouse%2Eio%2Facme%2Fjobs%2F4012&amp;urlHash=abc"--></code></html>"""

LINKEDIN_EASY = """<html><head><title>Beta Labs hiring AI Engineer | LinkedIn</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting", "title": "AI Engineer",
 "hiringOrganization": {"@type": "Organization", "name": "Beta Labs"}, "description": "&lt;p&gt;Ship LLM features.&lt;/p&gt;",
 "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX"}}}</script></head></html>"""

CAREERS_EMBED = """<html><title>Careers</title><div id="grnhse_app"></div>
<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script></html>"""


@pytest.mark.parametrize("url,expect", [
    ("https://job-boards.greenhouse.io/acme/jobs/4012", ("greenhouse", "acme", "4012")),
    ("https://boards.greenhouse.io/acme/jobs/4012?gh_src=x", ("greenhouse", "acme", "4012")),
    ("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=4012", ("greenhouse", "acme", "4012")),
    ("https://jobs.lever.co/acme/3f2b9c1e-1111-2222-3333-444455556666/apply",
     ("lever", "acme", "3f2b9c1e-1111-2222-3333-444455556666")),
    ("https://jobs.ashbyhq.com/openai/967f94aa-1706-4dba-ac89-bfbc2c38b688/application",
     ("ashby", "openai", "967f94aa-1706-4dba-ac89-bfbc2c38b688")),
    ("https://apply.workable.com/acme/j/AB12CD34EF/", ("workable", "acme", "AB12CD34EF")),
    ("https://example.com/careers/job?gh_jid=4012" + CAREERS_EMBED, ("greenhouse", "acme", "4012")),
    ("https://example.com/careers", None),
])
def test_board_links_are_recognised(url, expect):
    assert paste.board_link(url) == expect


@pytest.mark.parametrize("url,jid", [
    ("https://www.linkedin.com/jobs/view/4012345678/", "4012345678"),
    ("https://www.linkedin.com/jobs/view/forward-deployed-engineer-at-acme-ai-4012345678?trk=x", "4012345678"),
    ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4012345678", "4012345678"),
    ("https://www.linkedin.com/in/someone", None),
])
def test_linkedin_job_ids(url, jid):
    assert paste.linkedin_job_id(url) == jid


def test_linkedin_offsite_apply_and_job_ld():
    assert paste.linkedin_apply_url(LINKEDIN_OFFSITE) == "https://job-boards.greenhouse.io/acme/jobs/4012"
    assert paste.linkedin_apply_url(LINKEDIN_EASY) is None
    ld = paste.job_ld(LINKEDIN_EASY)
    assert ld and ld["title"] == "AI Engineer" and ld["hiringOrganization"]["name"] == "Beta Labs"


@pytest.fixture
def net(monkeypatch):
    pages: dict[str, str] = {}

    async def board(source, b, ext):
        return dict(POSTING) if (source, b, ext) == ("greenhouse", "acme", "4012") else None

    async def page(url):
        return url, pages[url]

    monkeypatch.setattr(paste, "fetch_board_posting", board)
    monkeypatch.setattr(paste, "_page", page)
    return pages


@pytest_asyncio.fixture(loop_scope="module")
async def jobs_db(monkeypatch):
    async with livedb.shadow(monkeypatch, "job_postings", "job_applications", "job_events", "job_queue",
                             "job_submissions", "notifications"):
        yield


@aio
async def test_a_board_link_is_tracked_and_queued_once(net, jobs_db):
    first = await paste.take("https://job-boards.greenhouse.io/acme/jobs/4012")
    assert first["applies"] and not first["duplicate"] and first["via"] == "board"
    app_row = await db.fetchrow("select source, status, url from job_applications where id = $1", first["application_id"])
    assert app_row["source"] == "pasted" and app_row["status"] == "interested"
    events = [r["kind"] for r in await db.fetch("select kind from job_events where application_id = $1", first["application_id"])]
    assert "pasted" in events
    again = await paste.take("https://boards.greenhouse.io/acme/jobs/4012")
    assert again["duplicate"] and again["queue_id"] == first["queue_id"]
    assert await db.fetchval("select count(*) from job_queue") == 1


@aio
async def test_linkedin_applies_on_the_company_board_when_it_links_one(net, jobs_db):
    net["https://www.linkedin.com/jobs/view/4012345678"] = LINKEDIN_OFFSITE
    out = await paste.take("https://www.linkedin.com/jobs/view/4012345678/")
    assert out["via"] == "linkedin" and out["applies"] and out["company"] == "Acme AI"


@aio
async def test_linkedin_easy_apply_is_tracked_never_automated(net, jobs_db):
    net["https://www.linkedin.com/jobs/view/4099999999"] = LINKEDIN_EASY
    out = await paste.take("https://www.linkedin.com/jobs/view/4099999999")
    assert not out["applies"] and out["company"] == "Beta Labs" and out["title"] == "AI Engineer"
    assert any("never automates" in f for f in out["flags"])
    src = await db.fetchval("select p.source from job_queue q join job_postings p on p.id = q.posting_id where q.id = $1",
                            out["queue_id"])
    assert src == "manual"


@aio
async def test_a_careers_page_embedding_a_board_is_followed(net, jobs_db):
    url = "https://example.com/careers/job?gh_jid=4012"
    net[url] = CAREERS_EMBED
    out = await paste.take(url)
    assert out["via"] == "careers page" and out["applies"]


@aio
async def test_a_careers_redirect_that_drops_the_job_number_still_finds_it(net, jobs_db, monkeypatch):
    """databricks.com/...job?gh_jid=N redirects to a pretty URL without gh_jid and
    loads Greenhouse by script: the number comes from the pasted link, the
    board from the site's name."""
    async def page(url):
        return "https://www.acme.com/careers/forward-deployed-engineer", "<html><title>FDE</title></html>"

    monkeypatch.setattr(paste, "_page", page)
    out = await paste.take("https://acme.com/company/careers/job?gh_jid=4012")
    assert out["via"] == "careers page" and out["applies"] and out["company"] == "Acme AI"


@aio
async def test_a_page_with_no_company_or_role_asks_for_them(net, jobs_db):
    url = "https://example.com/opening"
    net[url] = "<html><body>We are hiring.</body></html>"
    with pytest.raises(paste.PasteError, match="company and role"):
        await paste.take(url)
    out = await paste.take(url, company="Gamma", title="ML Engineer")
    assert out["company"] == "Gamma" and not out["applies"]


@aio
@pytest.mark.parametrize("bad", ["ftp://x.com/job", "javascript:alert(1)", "not a link"])
async def test_only_web_links(bad):
    with pytest.raises(paste.PasteError):
        await paste.find(bad)


@aio
async def test_draft_then_apply_only_when_asked_and_fillable(monkeypatch):
    calls: list[str] = []

    async def draft(item):
        calls.append("draft")
        return "ready"

    async def decide(qid, decision):
        calls.append(decision)
        return {"id": qid}

    async def submit(qid, **_: Any):
        calls.append("submit")
        return {"status": "dry_run"}

    async def event(*_: Any):
        return None

    monkeypatch.setattr(queue, "draft", draft)
    monkeypatch.setattr(queue, "decide", decide)
    monkeypatch.setattr(submitter, "submit_one", submit)
    monkeypatch.setattr(paste.store, "record_event", event)
    item = {"queue_id": "q", "posting_id": "p", "application_id": "a", "applies": True}
    assert await paste.draft_and_apply(item, apply=True) == "dry_run" and calls == ["draft", "apply", "submit"]
    calls.clear()
    assert await paste.draft_and_apply(item, apply=False) == "ready" and calls == ["draft"]
    calls.clear()
    assert await paste.draft_and_apply({**item, "applies": False}, apply=True) == "ready" and calls == ["draft"]


@aio
async def test_the_api_says_why_a_link_will_not_do(monkeypatch):
    async def refuse(url, **_: Any):
        raise paste.PasteError("the board does not list that job any more; it may have closed")

    monkeypatch.setattr(paste, "take", refuse)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.post("/api/jobs/paste", json={"url": "https://job-boards.greenhouse.io/acme/jobs/1"},
                         headers={"X-Atlas-Client": "1"})
    assert r.status_code == 400 and "may have closed" in r.json()["detail"]
