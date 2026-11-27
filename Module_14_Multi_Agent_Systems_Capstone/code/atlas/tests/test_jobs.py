"""The job search engine (R9b): rules, filters, boards, tracker, follow-ups.

Pure logic needs nothing; board fetchers run through an httpx MockTransport;
the tracker tests use the live test database and remove every row whose
company starts with 'Testco'.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from core import db
from core.jobs import discovery, followups, mail, store
from tests import livedb

PROFILE = {
    "titles": ["forward deployed", "ai solutions", "applied ai", "ai engineer", "solutions engineer"],
    "exclude_titles": ["director", "manager", "intern", "sales"],
    "locations": ["houston", "texas", "remote", "united states"],
    "salary_floor_usd": 110000,
}


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # clear_scores() and score_new() work on every open posting; on the live
    # database they wiped Trilok's real scores (2026-10-10). The job tables
    # are private copies here.
    async with livedb.shadow(monkeypatch, "job_postings", "job_applications", "job_events", "job_queue"):
        yield


# ------------------------------------------------------------------ pure


def test_keys_and_normalizing():
    assert store.norm("Bain & Company, Inc.") == "bain"
    assert store.app_key("Acme Corp", "AI Engineer") == store.app_key("acme", "ai  engineer")


@pytest.mark.parametrize(
    "current,proposed,expected",
    [
        (None, "applied", "applied"),
        ("applied", "interview", "interview"),
        ("interview", "applied", "interview"),  # never backwards
        ("interview", "rejected", "rejected"),
        ("rejected", "interview", "rejected"),  # a late invite does not undo a no
        ("rejected", "offer", "offer"),
        ("ghosted", "screening", "screening"),  # a reply revives it
        ("interview", "ghosted", "interview"),
    ],
)
def test_status_only_moves_forward(current, proposed, expected):
    assert store.next_status(current, proposed) == expected


@pytest.mark.parametrize(
    "sender,subject,snippet,status,company,role",
    [
        ("Bain & Company <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Bain & Company",
         "We have received your application for the Lead, Engineering Excellence position.",
         "applied", "Bain & Company", "Lead, Engineering Excellence"),
        ("Deloitte Careers <careers@myworkday.com>", "Thank you for your application",
         "Thank you for your interest in the Forward Deployed Engineer role at Deloitte.",
         "applied", "Deloitte", "Forward Deployed Engineer"),
        ("Indeed Apply <indeedapply@indeed.com>", "Indeed Application: AI Solutions Architect",
         "Your application has been submitted to Acme Automation. Good luck!",
         "applied", "Acme Automation", "AI Solutions Architect"),
        ("Sarah at Dataminr <no-reply@ashbyhq.com>", "Next steps with Dataminr",
         "We'd like to schedule a call to discuss the Forward Deployed Engineer role.",
         "interview", "Dataminr", "Forward Deployed Engineer"),
        ("Acme Recruiting <talent@acme.com>", "Your application to Acme",
         "Unfortunately, we have decided to move forward with other candidates.", "rejected", "Acme", None),
        ("Glean <no-reply@hire.lever.co>", "Offer letter from Glean", "We are pleased to extend an offer.",
         "offer", "Glean", None),
    ],
)
def test_classify_job_mail(sender, subject, snippet, status, company, role):
    v = mail.classify(sender, subject, snippet)
    assert v.relevant and v.status == status and v.company == company
    if role:
        assert v.role == role


@pytest.mark.parametrize(
    "sender,subject,snippet",
    [
        ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "AI Engineer: Dataminr is hiring", "new jobs for you"),
        ("Chris Doe <person7@gmail.com>", "Re: portal", "Unfortunately I can't make it, next steps tomorrow?"),
        ("Taylor Fox <person8@gmail.com>", "Hearing", "The interview with the trustee is Tuesday."),
    ],
)
def test_ordinary_mail_is_not_job_mail(sender, subject, snippet):
    assert not mail.classify(sender, subject, snippet).relevant


def test_a_confirmation_that_says_unfortunately_is_not_a_rejection():
    v = mail.classify(
        "Testco <no-reply@greenhouse.io>", "Thank you for applying to Testco",
        "Unfortunately, due to volume we cannot respond to every applicant individually.",
    )
    assert v.status == "applied"
    no = mail.classify("Testco <no-reply@greenhouse.io>", "Your application to Testco",
                       "We regret to inform you that we will not be moving forward.")
    assert no.status == "rejected"


def test_gmail_query_names_the_ats_senders():
    q = mail.gmail_query(14)
    assert q.startswith("newer_than:14d") and "from:greenhouse.io" in q and "-in:sent" in q


@pytest.mark.parametrize(
    "title,ok",
    [("Forward Deployed Engineer", True), ("Senior AI Solutions Engineer", True),
     ("Director, Applied AI", False), ("Sales Engineer", False), ("Backend Engineer", False)],
)
def test_title_filter(title, ok):
    assert discovery.title_ok(title, PROFILE) is ok


@pytest.mark.parametrize(
    "loc,remote,ok",
    [("Houston, TX", False, True), ("Austin, Texas", False, True), ("Remote - US", True, True),
     ("Remote (Canada)", True, False), ("London, UK", False, False), ("San Francisco, CA", False, False),
     ("Remote", None, True), ("Remote - Europe or US", True, True)],
)
def test_location_filter(loc, remote, ok):
    assert discovery.location_ok(loc, remote, PROFILE) is ok


def test_salary_parsing_and_floor():
    assert discovery.salary_from_text("The range is $150,000 - $210,000 plus equity") == (150000, 210000)
    assert discovery.salary_from_text("$140k-$180k") == (140000, 180000)
    assert discovery.salary_from_text("competitive pay") == (None, None)
    assert not discovery.salary_ok(90000, PROFILE) and discovery.salary_ok(None, PROFILE)


def test_followup_plan():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    apps = [
        {"id": "a", "status": "applied", "applied_at": (now - timedelta(days=9)).isoformat()},
        {"id": "b", "status": "applied", "applied_at": (now - timedelta(days=3)).isoformat()},
        {"id": "c", "status": "applied", "applied_at": (now - timedelta(days=40)).isoformat()},
        {"id": "d", "status": "interview", "applied_at": (now - timedelta(days=9)).isoformat(), "materials": {}},
        {"id": "e", "status": "applied", "applied_at": (now - timedelta(days=9)).isoformat(), "next_action": "x"},
        {"id": "f", "status": "screening", "applied_at": (now - timedelta(days=20)).isoformat(),
         "last_contact_at": (now - timedelta(days=2)).isoformat()},
    ]
    plan = followups.plan(apps, now)
    assert [a["id"] for a in plan["follow_up"]] == ["a"] and plan["follow_up"][0]["quiet_days"] == 9
    assert [a["id"] for a in plan["ghost"]] == ["c"]
    assert [a["id"] for a in plan["prep"]] == ["d"]


# ------------------------------------------------------------------ boards


def _board_client():
    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "greenhouse.io/v1/boards/testco/jobs/11" in u:
            return httpx.Response(200, json={"content": "&lt;p&gt;Build agents. Pay $150,000 - $190,000.&lt;/p&gt;"})
        if "greenhouse.io/v1/boards/testco/jobs" in u:
            return httpx.Response(200, json={"jobs": [
                {"id": 11, "title": "Forward Deployed Engineer", "location": {"name": "Remote - US"}, "absolute_url": "https://x/11"},
                {"id": 12, "title": "Account Executive", "location": {"name": "Remote - US"}, "absolute_url": "https://x/12"},
                {"id": 13, "title": "Applied AI Engineer", "location": {"name": "London"}, "absolute_url": "https://x/13"},
            ]})
        if "api.lever.co/v0/postings/testco-lever" in u:
            return httpx.Response(200, json=[
                {"id": "L1", "text": "AI Solutions Engineer", "categories": {"location": "Houston, TX"},
                 "hostedUrl": "https://l/1", "createdAt": 1760000000000, "descriptionPlain": "Python and LLMs",
                 "salaryRange": {"min": 120000, "max": 160000, "currency": "USD"}},
                {"id": "L2", "text": "AI Engineer", "categories": {"location": "Remote"}, "workplaceType": "remote",
                 "hostedUrl": "https://l/2", "salaryRange": {"min": 60000, "max": 80000, "currency": "USD"}},
            ])
        if "ashbyhq.com/posting-api/job-board/testco-ashby" in u:
            return httpx.Response(200, json={"jobs": [
                {"id": "A1", "title": "Applied AI Engineer", "location": "New York", "isRemote": True,
                 "jobUrl": "https://a/1", "descriptionPlain": "Agents for customers",
                 "compensation": {"summaryComponents": [{"compensationType": "Salary", "currencyCode": "USD",
                                                         "minValue": 170000, "maxValue": 220000}]}},
            ]})
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio(loop_scope="module")
async def test_fetchers_parse_each_board_and_report_a_missing_one():
    async with _board_client() as c:
        gh, rep = await discovery.fetch_board(c, {"name": "Testco GH", "source": "greenhouse", "board": "testco"})
        assert rep == "ok 3" and gh[0]["title"] == "Forward Deployed Engineer" and gh[0]["remote"] is True
        lv, _ = await discovery.fetch_board(c, {"name": "Testco Lever", "source": "lever", "board": "testco-lever"})
        assert lv[0]["salary_min"] == 120000 and lv[0]["location"] == "Houston, TX"
        ab, _ = await discovery.fetch_board(c, {"name": "Testco Ashby", "source": "ashby", "board": "testco-ashby"})
        assert ab[0]["remote"] is True and ab[0]["salary_max"] == 220000
        _, rep = await discovery.fetch_board(c, {"name": "Nope", "source": "greenhouse", "board": "nope"})
        assert rep.startswith("not found")


@pytest.mark.asyncio(loop_scope="module")
async def test_discover_keeps_only_fitting_roles_and_is_idempotent():
    companies = [
        {"name": "Testco GH", "source": "greenhouse", "board": "testco"},
        {"name": "Testco Lever", "source": "lever", "board": "testco-lever"},
        {"name": "Testco Ashby", "source": "ashby", "board": "testco-ashby"},
    ]
    async with _board_client() as c:
        first = await discovery.discover(companies, PROFILE, client=c, score=False)
        again = await discovery.discover(companies, PROFILE, client=c, score=False)
    # Kept: the GH forward deployed role, the Houston Lever role, the remote Ashby role.
    # Dropped: the sales title, London, and the Lever role under the salary floor.
    assert first["kept"] == 3 and first["new"] == 3
    assert again["new"] == 0
    row = await db.fetchrow("select * from job_postings where board = 'testco' and external_id = '11'")
    assert row["salary_min"] == 150000 and "Build agents" in row["description"]


@pytest.mark.asyncio(loop_scope="module")
async def test_scoring_writes_scores_from_the_model():
    pid, _ = await store.upsert_posting({"source": "manual", "board": "t", "external_id": "s1", "company": "Testco Score",
                                         "title": "Applied AI Engineer"})

    async def fake(**kw):
        return {"scores": [{"id": str(pid), "score": 88, "reason": "Agents for clients, his core work."}]}

    with patch("core.llm.complete_json", new=fake), \
         patch("core.jobs.store.unscored_postings", new=AsyncMock(side_effect=[[await store.get_posting(pid)], []])):
        assert await discovery.score_new(PROFILE) == 1
    p = await store.get_posting(pid)
    assert p["score"] == 88 and p["reason"].startswith("Agents")


# ------------------------------------------------------------------ tracker


@pytest.mark.asyncio(loop_scope="module")
async def test_tracker_upsert_find_and_event_dedupe():
    a = await store.upsert_application("Testco Alpha", "AI Engineer", source="gmail", thread_id="t1")
    assert a["status"] == "applied"
    again = await store.upsert_application("Testco Alpha Inc.", "AI  Engineer", status="interview")
    assert again["id"] == a["id"] and again["status"] == "interview"
    assert (await store.find_application("Testco Alpha", None, "t1"))["id"] == a["id"]
    assert (await store.find_application("testco alpha"))["id"] == a["id"]  # one open app at that company
    assert await store.record_event(a["id"], "invite", "x", message_id="testco-m1") is True
    assert await store.record_event(a["id"], "invite", "x", message_id="testco-m1") is False
    later = await store.upsert_application("Testco Alpha", "AI Engineer", status="applied")
    assert later["status"] == "interview"


@pytest.mark.asyncio(loop_scope="module")
async def test_seed_from_config_is_idempotent():
    cfg = {"applications": [{"company": "Testco Seed", "role": "FDE", "applied": "2026-10-07"}]}
    assert await store.seed_from_config(cfg) == 1
    assert await store.seed_from_config(cfg) == 0
    row = await db.fetchrow("select * from job_applications where company = 'Testco Seed'")
    assert row["applied_at"].date().isoformat() == "2026-10-07"


class _FakeGmail:
    def __init__(self, messages):
        self.messages = messages

    async def _get(self, path, params=None):
        if path == "/messages":
            return {"messages": [{"id": m["id"]} for m in self.messages]}
        mid = path.rsplit("/", 1)[-1]
        m = next(x for x in self.messages if x["id"] == mid)
        return {
            "id": mid, "threadId": m["thread"], "internalDate": str(m["ms"]), "snippet": m["snippet"],
            "payload": {"headers": [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]}]},
        }


@pytest.mark.asyncio(loop_scope="module")
async def test_scan_files_mail_moves_status_and_announces_once():
    base = int(datetime(2026, 10, 7, tzinfo=UTC).timestamp() * 1000)
    msgs = [  # Gmail lists newest first
        {"id": "testco-2", "thread": "th-9", "ms": base + 86400000, "from": "Testco Beta <no-reply@ashbyhq.com>",
         "subject": "Interview invitation - Testco Beta",
         "snippet": "Please share your availability for an interview for the Forward Deployed Engineer position."},
        {"id": "testco-1", "thread": "th-9", "ms": base, "from": "Testco Beta <no-reply@ashbyhq.com>",
         "subject": "Thank you for applying to Testco Beta",
         "snippet": "We received your application for the Forward Deployed Engineer position."},
    ]
    note = AsyncMock()
    with patch("core.surfaces.notify.notify", new=note):
        s1 = await mail.scan(_FakeGmail(msgs))
        s2 = await mail.scan(_FakeGmail(msgs))
    assert s1["created"] == 1 and s1["filed"] == 2 and s1["moved"] == 1
    assert s2["filed"] == 0
    row = await db.fetchrow("select * from job_applications where company = 'Testco Beta'")
    assert row["status"] == "interview" and row["role"] == "Forward Deployed Engineer"
    note.assert_awaited_once()
    assert note.await_args.kwargs["title"] == "Interview: Testco Beta"


@pytest.mark.asyncio(loop_scope="module")
async def test_daily_pass_sets_follow_ups_and_ghosts():
    old = datetime.now(UTC) - timedelta(days=10)
    a = await store.upsert_application("Testco Quiet", "AI Engineer", applied_at=old)
    b = await store.upsert_application("Testco Gone", "AI Engineer", applied_at=datetime.now(UTC) - timedelta(days=45))
    note = AsyncMock()
    with patch("core.surfaces.notify.notify", new=note):
        res = await followups.run_daily(prep=False)
    assert res["follow_up"] >= 1 and res["ghosted"] >= 1
    assert (await store.get_application(a["id"]))["next_action"].startswith("Follow up")
    assert (await store.get_application(b["id"]))["status"] == "ghosted"


@pytest.mark.asyncio(loop_scope="module")
async def test_brief_block_lists_fresh_roles_once():
    pid, _ = await store.upsert_posting({"source": "manual", "board": "t", "external_id": "b1", "company": "Testco Brief",
                                         "title": "Forward Deployed Engineer", "url": "https://x"})
    await store.score_posting(pid, 91, "Exactly his work.")
    with patch("core.jobs.config.profile", return_value={"brief_min_score": 70}):
        first = await followups.brief_block()
        second = await followups.brief_block()
    assert "Forward Deployed Engineer at Testco Brief (91" in first
    assert "Testco Brief" not in second


# ------------------------------------------------------------------ agent tools


@pytest.mark.asyncio(loop_scope="module")
async def test_career_tools_guard_their_inputs(tmp_path, monkeypatch):
    from agents.career import tools as t
    from core.jobs import config as jc

    monkeypatch.setattr(jc, "RESUME_DIR", tmp_path)
    r = await t._read_resume({})
    assert r["resume"] is None and "data\\resume" in r["note"]
    (tmp_path / "resume.md").write_text("Trilok Kalani\nAI engineer", encoding="utf-8")
    assert (await t._read_resume({}))["resume"].startswith("Trilok")
    assert "kind must be" in (await t._save_materials({"application_id": "x", "kind": "essay", "text": "t"}))["error"]
    app = await store.upsert_application("Testco Draft", "AI Engineer")
    ok = await t._save_materials({"application_id": app["id"], "kind": "cover", "text": "Dear team"})
    assert ok == {"saved": "cover", "application_id": app["id"]}
    assert (await store.get_application(app["id"]))["materials"]["cover"]["text"] == "Dear team"


@pytest.mark.asyncio(loop_scope="module")
async def test_workable_board_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "apply.workable.com/api/v1/widget/accounts/testco-wk" in str(request.url)
        return httpx.Response(200, json={"jobs": [
            {"title": "Applied AI Engineer", "shortcode": "W1", "url": "https://apply.workable.com/j/W1",
             "city": "", "state": "", "country": "United States", "telecommuting": True,
             "published_on": "2026-10-01", "description": "<p>Ship agents. $150,000 - $180,000</p>"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        posts, rep = await discovery.fetch_board(c, {"name": "Testco WK", "source": "workable", "board": "testco-wk"})
    assert rep == "ok 1"
    p = posts[0]
    assert p["remote"] is True and p["location"] == "Remote; United States" and p["salary_max"] == 180000
    assert discovery.keep(p, PROFILE)


@pytest.mark.asyncio(loop_scope="module")
async def test_scoring_runs_until_nothing_is_pending():
    ids = []
    for i in range(20):
        pid, _ = await store.upsert_posting({"source": "manual", "board": "t", "external_id": f"r{i}",
                                             "company": "Testco Rounds", "title": "Applied AI Engineer"})
        ids.append(str(pid))
    calls = 0

    async def fake(**kw):
        nonlocal calls
        calls += 1
        found = [line.split("id=")[1].split()[0] for line in kw["user"].split("### ") if "id=" in line]
        return {"scores": [{"id": i, "score": 50, "reason": "ok"} for i in found]}

    with patch("core.llm.complete_json", new=fake):
        n = await discovery.score_new(PROFILE, batch=8)
    left = await db.fetchval("select count(*) from job_postings where company = 'Testco Rounds' and score is null")
    assert n >= 20 and left == 0 and calls >= 3


@pytest.mark.asyncio(loop_scope="module")
async def test_clear_scores_reopens_open_postings_only():
    a, _ = await store.upsert_posting({"source": "manual", "board": "t", "external_id": "c1", "company": "Testco Clear", "title": "AI Engineer"})
    b, _ = await store.upsert_posting({"source": "manual", "board": "t", "external_id": "c2", "company": "Testco Clear", "title": "AI Engineer"})
    await store.score_posting(a, 40, "x")
    await store.score_posting(b, 40, "x")
    await store.set_posting_status(b, "dismissed")
    await store.clear_scores()
    assert (await store.get_posting(a))["score"] is None
    assert (await store.get_posting(b))["score"] == 40
