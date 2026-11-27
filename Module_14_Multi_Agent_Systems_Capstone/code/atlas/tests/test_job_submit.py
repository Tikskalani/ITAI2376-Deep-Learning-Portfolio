"""The job submitter (auto-apply PR D): the pre-submit check, the Greenhouse
adapter, and the submit path end to end.

No browser and no network: the form is a FakePage, board liveness goes
through an httpx MockTransport, and saved answers are patched. The job
tables are private copies (tests/livedb.py), so Trilok's real postings,
queue and applications are never read or touched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db
from core.jobs import answers, precheck, store, submitter
from core.jobs.submit import greenhouse
from core.jobs.submit.base import FormField, confirmed, obstacle, pick_option
from core.web.api import app
from tests import livedb

MASTER = """Jane Doe
Houston, TX | jane@example.com | 713 555 0100
Experience
Acme Robotics, Houston, TX | 2024 to present
Built 12 agent workflows that cut ticket time by 40%.
Northwind Logistics, Austin, TX | 2016 to 2024
Ran operations for 3 warehouses.
Education
Houston Community College | Expected Dec 2026
"""

RESUME = """Changed: led with the agent work.
# Jane Doe
Houston, TX | jane@example.com | 713 555 0100
## Summary
Operations lead turned AI engineer.
## Experience
### Acme Robotics, Houston, TX | 2024 to present
AI Engineer
- Built 12 agent workflows that cut ticket time by 40%.
### Northwind Logistics, Austin, TX | 2016 to 2024
- Ran operations for 3 warehouses.
## Education
### Houston Community College | Expected Dec 2026
AAS, Artificial Intelligence
"""

COVER = (
    "Dear Testco Labs team,\n\nI am applying for the Applied AI Engineer role. At Acme Robotics I built "
    "12 agent workflows.\n\nJane Doe"
)

aio = pytest.mark.asyncio(loop_scope="module")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _check(**over: Any) -> precheck.Result:
    kw: dict[str, Any] = dict(company="Testco Labs", title="Applied AI Engineer", resume_text=RESUME,
                              cover=COVER, master=MASTER, other_companies=["Globex", "Initech"])
    kw.update(over)
    return precheck.check(**kw)


# ------------------------------------------------------------------ the pre-submit check


def test_clean_materials_pass():
    r = _check()
    assert r.ok, r.problems


@pytest.mark.parametrize("cover,expect", [
    (COVER.replace("Testco Labs", "{{company}}"), "placeholder"),
    (COVER + "\nI can start [date?].", "placeholder"),
    (COVER + "\nTODO: add a closing line.", "placeholder"),
    (COVER.replace("Testco Labs", "Globex"), "never names Testco Labs"),
    (COVER + "\nI loved my chat with the Initech team.", "names another company: Initech"),
    (COVER.replace("Applied AI Engineer", "Barista"), "does not name the role"),
    ("", "cover note is empty"),
])
def test_cover_problems_are_caught(cover, expect):
    r = _check(cover=cover)
    assert not r.ok and any(expect in p for p in r.problems), r.problems


def test_invented_numbers_and_employers_are_caught():
    r = _check(resume_text=RESUME.replace("cut ticket time by 40%", "cut ticket time by 65%"))
    assert any("numbers the master resume does not: 65" in p for p in r.problems)
    r = _check(resume_text=RESUME.replace("Northwind Logistics", "Stark Industries"))
    assert any("'Stark Industries'" in p for p in r.problems)


def test_a_former_employer_is_not_another_company_and_no_master_fails_closed():
    assert _check(other_companies=["Acme Robotics"]).ok
    r = _check(master="")
    assert any("no master resume" in p for p in r.problems)


def test_resume_shape_problems():
    assert any("does not parse" in p for p in _check(resume_text="no heading here").problems)
    long = RESUME + "\n".join(f"- Built 12 agent workflows, again and again, number {i}." for i in range(1, 2))
    long += "\n" + "\n".join("- Ran operations for 3 warehouses with care and attention." for _ in range(90))
    assert any("pages" in p for p in _check(resume_text=long).problems)
    assert any("no real name" in p for p in _check(resume_text=RESUME.replace("# Jane Doe", "# Full Name")).problems)


# ------------------------------------------------------------------ options and page reading


def test_pick_option_matches_whole_words_and_refuses_to_guess():
    opts = ["Not specified", "No, I do not have a disability", "Yes, I have a disability"]
    assert pick_option("No", opts) == "No, I do not have a disability"
    assert pick_option("yes", ["Yes", "No"]) == "Yes"
    assert pick_option("Houston", ["Austin", "Dallas"]) is None
    assert pick_option("", ["Yes"]) is None


class FakePage:
    """A form that records what Atlas did to it."""

    def __init__(self, fields: list[FormField], *, after_submit: str = "Thank you for applying!",
                 challenge: bool = False, text: str = "Apply for this job", explode: str | None = None) -> None:
        self._fields = fields
        self._text = text
        self.after_submit = after_submit
        self.challenge = challenge
        self.explode = explode
        self.url = "https://job-boards.greenhouse.io/testco/jobs/1"
        self.opened: list[str] = []
        self.filled: dict[str, str] = {}
        self.uploads: dict[str, Path] = {}
        self.pressed = 0

    async def open(self, url: str) -> None:
        self.opened.append(url)

    async def fields(self) -> list[FormField]:
        return self._fields

    async def fill(self, ref: str, value: str) -> None:
        self.filled[ref] = value

    async def choose(self, ref: str, option: str) -> None:
        self.filled[ref] = option

    async def tick(self, ref: str, on: bool = True) -> None:
        self.filled[ref] = "on" if on else "off"

    async def upload(self, ref: str, path: Path) -> None:
        self.uploads[ref] = path

    async def screenshot(self, path: Path) -> None:
        _write(path, b"\x89PNG fake")

    async def press_submit(self) -> None:
        self.pressed += 1
        if self.explode == "submit":
            raise TimeoutError("the page stopped answering")
        self._text = self.after_submit

    async def text(self) -> str:
        return self._text

    async def challenge_visible(self) -> bool:
        return self.challenge


def _form(extra: list[FormField] | None = None) -> list[FormField]:
    return [
        FormField("f1", "First Name*", "text", True),
        FormField("f2", "Last Name*", "text", True),
        FormField("f3", "Email*", "email", True),
        FormField("f4", "Phone", "tel"),
        FormField("f5", "Resume/CV*", "file", True),
        FormField("f6", "Cover Letter", "file"),
        FormField("f7", "Are you legally authorized to work in the United States?*", "select", True, ["Yes", "No"]),
        FormField("f8", "Disability Status", "select", False, ["I don't wish to answer", "No, I do not have a disability"]),
        *(extra or []),
    ]


PROFILE = {"full_name": "Jane Q Doe", "email": "jane@example.com", "phone": "713 555 0100",
           "work_authorized_us": "Yes", "disability_status": "Decline to self-identify"}


@pytest.fixture
def saved_answers(monkeypatch):
    asked: list[str] = []

    async def form_profile():
        return dict(PROFILE)

    async def answer_for(question):
        q = answers.standard_for(question)
        return PROFILE.get(q.key) if q else None

    async def ask(question, context=None):
        asked.append(question)
        return answers.question_key(question)

    async def missing_required():
        return []

    monkeypatch.setattr(answers, "form_profile", form_profile)
    monkeypatch.setattr(answers, "answer_for", answer_for)
    monkeypatch.setattr(answers, "ask", ask)
    monkeypatch.setattr(answers, "missing_required", missing_required)
    return asked


@aio
async def test_greenhouse_fill_answers_by_label_and_reports_what_it_cannot(saved_answers, tmp_path):
    files = greenhouse.Files(tmp_path / "r.pdf", tmp_path / "c.txt", COVER)
    page = FakePage(_form([FormField("f9", "Why do you want to work here?*", "textarea", True)]))
    out = await greenhouse.fill(page, {}, files)
    assert page.filled["f1"] == "Jane Q" and page.filled["f2"] == "Doe" and page.filled["f3"] == "jane@example.com"
    assert page.filled["f7"] == "Yes"
    assert "f8" not in page.filled  # optional, no matching option: left alone, never guessed
    assert page.uploads == {"f5": files.resume, "f6": files.cover}
    assert out.missing == ["Why do you want to work here?*"]
    assert out.answers["Resume/CV*"] == "r.pdf"


@aio
async def test_page_reading_spots_walls_and_confirmations():
    assert await obstacle(FakePage([], challenge=True)) == "a CAPTCHA is on the page"
    assert "login" in (await obstacle(FakePage([], text="Please sign in to apply")) or "")
    assert "code" in (await obstacle(FakePage([], text="Enter the verification code we sent")) or "")
    assert await obstacle(FakePage([])) is None
    assert await confirmed(FakePage([], text="Thank you for applying to Testco"))
    assert not await confirmed(FakePage([], text="Apply for this job"))


class _Frame:
    def __init__(self, name: str, found: Any) -> None:
        self.name, self.found = name, found

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if isinstance(self.found, Exception):
            raise self.found
        return self.found

    def locator(self, selector: str) -> tuple[str, str]:
        return (self.name, selector)


@aio
async def test_the_browser_finds_a_form_embedded_in_an_iframe():
    """Databricks, 2026-10-23: the careers page holds a search box, the
    Greenhouse iframe holds the 22-field form."""
    from core.jobs.submit.browser import BrowserPage

    def field(i: int) -> dict[str, Any]:
        return {"ref": f"atlas-{i}", "label": f"Q{i}", "kind": "text", "required": False, "options": []}

    main = _Frame("page", [field(0)])
    pw = SimpleNamespace(main_frame=main, frames=[
        main, _Frame("ads", RuntimeError("frame was detached")), _Frame("form", [field(i) for i in range(3)]),
    ])
    page = BrowserPage(pw)
    assert [f.label for f in await page.fields()] == ["Q0", "Q1", "Q2"]
    assert page._el("atlas-1") == ("form", '[data-atlas-ref="atlas-1"]')
    # No iframe with more: the page itself.
    page = BrowserPage(SimpleNamespace(main_frame=main, frames=[main]))
    await page.fields()
    assert page._el("atlas-0")[0] == "page"


def test_application_url_prefers_the_greenhouse_page():
    assert greenhouse.application_url({"url": "https://boards.greenhouse.io/testco/jobs/9"}).endswith("/jobs/9")
    assert greenhouse.application_url({"url": "https://testco.com/careers/9", "board": "testco",
                                       "external_id": "9"}) == "https://job-boards.greenhouse.io/testco/jobs/9"


def test_settings_default_to_dry_run():
    assert submitter.settings({})["dry_run"] is True
    assert submitter.settings({"submit": {"dry_run": "no"}})["dry_run"] is True  # only false turns it off
    assert submitter.settings({"submit": {"dry_run": False}})["dry_run"] is False


# ------------------------------------------------------------------ the submit path


@pytest_asyncio.fixture(loop_scope="module")
async def jobs_db(monkeypatch, tmp_path, saved_answers):
    async with livedb.shadow(monkeypatch, "job_postings", "job_applications", "job_events", "job_queue",
                             "job_submissions", "candidate_answers", "notifications"):
        monkeypatch.setattr(submitter, "EVIDENCE_DIR", tmp_path / "applications")
        yield tmp_path


def _live() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 1})))


async def _approved(source: str = "greenhouse", cover: str = COVER, company: str = "Testco Labs") -> dict[str, Any]:
    pid, _ = await store.upsert_posting({
        "source": source, "board": "testco", "external_id": uuid4().hex[:8], "company": company,
        "title": "Applied AI Engineer", "url": None, "description": "Build agents.",
    })
    app_row = await store.upsert_application(company, "Applied AI Engineer", status="interested",
                                             source="queue", posting_id=pid)
    await store.save_materials(app_row["id"], {"resume": {"text": RESUME}, "cover": {"text": cover}})
    qid = await db.fetchval(
        "insert into job_queue (posting_id, application_id, status, decided_at) values ($1, $2, 'approved', now()) "
        "returning id", pid, app_row["id"],
    )
    return {"queue_id": qid, "posting_id": pid, "application_id": app_row["id"]}


def _pages(page: FakePage):
    @asynccontextmanager
    async def factory():
        yield page
    return factory


async def _submit(item, page, *, dry_run: bool):
    cfg = {**submitter.DEFAULTS, "dry_run": dry_run}
    async with _live() as client:
        return await submitter.submit_one(item["queue_id"], pages=_pages(page), cfg=cfg, master=MASTER, client=client)


@aio
async def test_dry_run_fills_screenshots_keeps_evidence_and_sends_nothing(jobs_db):
    item = await _approved()
    page = FakePage(_form())
    out = await _submit(item, page, dry_run=True)
    assert out["status"] == "dry_run" and page.pressed == 0
    sub = await db.fetchrow("select * from job_submissions where id = $1", out["submission_id"])
    ev = sub["evidence"][-1]
    assert ev["dry_run"] and ev["outcome"] == "dry_run" and ev["answers"]["First Name*"] == "Jane Q"
    resume = Path(ev["resume"]["path"])
    assert resume.name == "Jane Doe - Resume - Testco Labs.pdf" and _read(resume).startswith(b"%PDF")
    assert len(ev["resume"]["sha256"]) == 64 and _read(Path(ev["cover"]["path"])).startswith(b"Dear")
    assert ev["screenshots"] == [f"{sub['id']}/attempt-1/filled.png"]
    assert (resume.parent / "evidence.json").is_file()
    assert await db.fetchval("select status from job_queue where id = $1", item["queue_id"]) == "approved"
    # While dry run is on, a done dry run is not repeated by the scheduled run.
    assert item["queue_id"] not in await submitter.due({**submitter.DEFAULTS, "dry_run": True})
    assert item["queue_id"] in await submitter.due({**submitter.DEFAULTS, "dry_run": False})


class PagedPage(FakePage):
    """A browser that returns the form in three screen-high pages (shots.tile)."""

    async def screenshot(self, path: Path) -> list[Path]:
        pages = [path.with_name(f"{path.stem}-{n}.png") for n in (1, 2, 3)]
        for p in pages:
            _write(p, b"\x89PNG fake")
        return pages


@aio
async def test_every_page_of_the_form_is_kept_as_evidence(jobs_db):
    item = await _approved()
    out = await _submit(item, PagedPage(_form()), dry_run=True)
    sub = await db.fetchrow("select * from job_submissions where id = $1", out["submission_id"])
    assert sub["evidence"][-1]["screenshots"] == [f"{sub['id']}/attempt-1/filled-{n}.png" for n in (1, 2, 3)]


@aio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_page_with_no_form_is_never_called_filled(jobs_db, dry_run):
    """Cursor, 2026-10-23: Ashby's hosted page said "Page not found" and the
    dry run reported "filled". Nothing filled means a click for Trilok, and
    the submit button is never pressed."""
    item = await _approved()
    page = FakePage([], text="Page not found")
    out = await _submit(item, page, dry_run=dry_run)
    assert out["status"] == "needs_click" and out["reason"] == submitter.NO_FORM and page.pressed == 0
    ev = (await db.fetchrow("select evidence from job_submissions where id = $1", out["submission_id"]))["evidence"]
    assert ev[-1]["outcome"] == "no_form" and ev[-1]["screenshots"]


@aio
async def test_no_application_goes_out_without_the_resume(jobs_db):
    """Uploads labelled only "Attach" (Greenhouse's embedded form): the name
    and email fill, but no slot is known to take the resume, so Atlas stops."""
    item = await _approved()
    page = FakePage([FormField("f1", "First Name*", "text", True), FormField("f3", "Email*", "email", True),
                     FormField("f5", "Attach", "file"), FormField("f6", "Attach", "file")])
    out = await _submit(item, page, dry_run=False)
    assert out["status"] == "needs_click" and out["reason"] == submitter.NO_RESUME_SLOT
    assert page.pressed == 0 and page.uploads == {}


@aio
async def test_real_submit_happens_once_and_marks_everything_applied(jobs_db):
    item = await _approved()
    page = FakePage(_form())
    out = await _submit(item, page, dry_run=False)
    assert out["status"] == "submitted" and page.pressed == 1
    assert await db.fetchval("select status from job_queue where id = $1", item["queue_id"]) == "submitted"
    app_row = await store.get_application(item["application_id"])
    assert app_row["status"] == "applied" and app_row["applied_at"] is not None
    assert (await store.get_posting(item["posting_id"]))["status"] == "applied"
    sub = await db.fetchrow("select * from job_submissions where posting_id = $1", item["posting_id"])
    assert sub["adapter_ok"] is True and sub["submitted_at"] is not None
    assert sub["evidence"][-1]["screenshots"][-1].endswith("confirmation.png")

    # A second tap, a retry, a second worker: never a second submit.
    await db.execute("update job_queue set status = 'approved' where id = $1", item["queue_id"])
    again = await _submit(item, FakePage(_form()), dry_run=False)
    assert again["status"] == "submitted" and again["reason"] == "already submitted"
    assert page.pressed == 1


@aio
async def test_a_crash_after_pressing_submit_is_never_retried(jobs_db):
    item = await _approved()
    out = await _submit(item, FakePage(_form(), explode="submit"), dry_run=False)
    assert out["status"] == "submitting"
    again = await _submit(item, FakePage(_form()), dry_run=False)
    assert again["status"] == "submitting" and "outcome is unknown" in again["reason"]


@aio
async def test_walls_questions_and_failed_checks_come_back_to_trilok(jobs_db, saved_answers):
    captcha = await _approved()
    out = await _submit(captcha, FakePage(_form(), challenge=True), dry_run=False)
    assert out["status"] == "needs_click" and "CAPTCHA" in out["reason"]

    on_submit = await _approved()
    page = FakePage(_form(), after_submit="Please verify you are human")
    out = await _submit(on_submit, page, dry_run=False)
    assert out["status"] == "needs_click" and page.pressed == 1
    assert await db.fetchval("select status from job_queue where id = $1", on_submit["queue_id"]) == "approved"

    question = await _approved()
    out = await _submit(question, FakePage(_form([FormField("q", "Why Testco?*", "textarea", True)])), dry_run=False)
    assert out["status"] == "needs_answer" and saved_answers == ["Why Testco?*"]

    bad = await _approved(cover=COVER + "\nTODO")
    out = await _submit(bad, FakePage(_form()), dry_run=False)
    assert out["status"] == "blocked" and "placeholder" in out["reason"]
    row = await db.fetchrow("select status, error from job_queue where id = $1", bad["queue_id"])
    assert row["status"] == "needs_attention" and row["error"].startswith("Pre-submit check:")

    manual = await _approved(source="manual")
    out = await _submit(manual, FakePage(_form()), dry_run=False)
    assert out["status"] == "needs_click" and out["reason"].startswith("Atlas does not fill this posting's form.")


@aio
async def test_lever_ashby_and_workable_are_filled_like_greenhouse(jobs_db, monkeypatch):
    """The three boards share Greenhouse's fill; each gets its own form URL.
    (Whether a posting is still open is each board's own check, tested with
    discovery; here every posting is open.)"""
    from core.jobs import discovery
    from core.jobs.submit import ADAPTERS, boards

    async def open_posting(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(discovery, "is_live", open_posting)

    assert set(ADAPTERS) == {"greenhouse", "lever", "ashby", "workable"}
    assert boards.lever_url({"url": "https://jobs.lever.co/acme/abc-123", "board": "acme", "external_id": "abc-123"}) \
        == "https://jobs.lever.co/acme/abc-123/apply"
    assert boards.lever_url({"url": "https://jobs.lever.co/acme/abc-123/apply?src=x"}) == "https://jobs.lever.co/acme/abc-123/apply"
    assert boards.ashby_url({"url": None, "board": "acme", "external_id": "j-9"}) == "https://jobs.ashbyhq.com/acme/j-9/application"
    assert boards.ashby_url({"url": "https://jobs.ashbyhq.com/acme/j-9/"}) == "https://jobs.ashbyhq.com/acme/j-9/application"
    assert boards.workable_url({"url": "https://apply.workable.com/acme/j/AB12CD/"}) == "https://apply.workable.com/acme/j/AB12CD/apply/"
    assert boards.workable_url({"url": "https://elsewhere.test/x", "board": "acme", "external_id": "AB12CD"}) \
        == "https://apply.workable.com/acme/j/AB12CD/apply/"

    for source in ("lever", "ashby"):
        item = await _approved(source=source)
        page = FakePage(_form())
        out = await _submit(item, page, dry_run=True)
        assert out["status"] == "dry_run", (source, out)
        assert page.pressed == 0, "dry run never presses submit"


@aio
async def test_an_adapter_that_keeps_failing_is_switched_off(jobs_db):
    for _ in range(5):
        item = await _approved()
        out = await _submit(item, FakePage(_form(), after_submit="Something went wrong"), dry_run=False)
        assert out["status"] == "needs_click" and "no confirmation" in out["reason"]
    item = await _approved()
    page = FakePage(_form())
    out = await _submit(item, page, dry_run=False)
    assert out["status"] == "needs_click" and "only 0 of its last 5" in out["reason"]
    assert page.opened == []  # did not even open the form


@aio
async def test_routes_show_the_submission_and_serve_its_screenshots(jobs_db, monkeypatch):
    from core.jobs import queue as apply_queue

    item = await _approved()
    out = await _submit(item, FakePage(_form()), dry_run=True)
    # The listing only shows open or today's items; this one is approved, so it shows.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/jobs/queue")
        mine = [i for i in r.json()["items"] if i["id"] == str(item["queue_id"])]
        assert mine and mine[0]["submission"]["status"] == "dry_run" and r.json()["dry_run"] is True
        shot = mine[0]["submission"]["screenshots"][0]
        assert shot == "attempt-1/filled.png"
        f = await c.get(f"/api/jobs/submissions/{out['submission_id']}/files/{shot}")
        assert f.status_code == 200 and f.content.startswith(b"\x89PNG")
        assert (await c.get(f"/api/jobs/submissions/{out['submission_id']}/files/../../secret")).status_code == 404
        assert (await c.get("/api/jobs/submissions/not-a-uuid/files/x.png")).status_code == 404
    assert apply_queue  # the listing route reads the real queue module


@aio
async def test_apply_and_apply_all_start_the_submitter(jobs_db, monkeypatch):
    from core.web import jobs as jobs_routes

    started: list[str] = []

    def record(coro) -> bool:
        started.append(coro.__qualname__)
        coro.close()
        return True

    monkeypatch.setattr(jobs_routes, "_start_submit", record)
    item = await _approved()
    await db.execute("update job_queue set status = 'ready', decided_at = null where id = $1", item["queue_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.post(f"/api/jobs/queue/{item['queue_id']}/decide", json={"decision": "apply"})
        assert r.status_code == 200 and started == ["run"]
        assert (await c.post(f"/api/jobs/queue/{item['queue_id']}/submit")).json()["started"] is True
    assert started == ["run", "submit_one"]


def test_submitter_reasons_read_as_cause_and_fix():
    raw = ("the form could not be filled: Error: BrowserType.launch_persistent_context: Executable doesn't exist at "
           r"C:\Users\x\ms-playwright\chromium_headless_shell-1243\chrome.exe")
    assert submitter.plain_reason(raw, "greenhouse") == submitter.BROWSER_MISSING
    old = "Atlas cannot fill ashby forms yet; the materials are ready"
    assert submitter.plain_reason(old, "ashby").startswith("Atlas does not fill Ashby forms.")
    assert submitter.plain_reason("the form could not be filled: TimeoutError: page took too long", "greenhouse") \
        == "the form could not be filled: page took too long"
    assert submitter.plain_reason("submit was pressed but no confirmation appeared", None) \
        == "submit was pressed but no confirmation appeared"
    assert submitter.plain_reason(None) is None
