"""The privacy gate: a private signal's content never reaches a model, on any path.

Fixtures only (.test domains); nothing here reads a real mailbox or calls a model.
A spy stands in for core.llm (complete and complete_json) and for the agent
dispatch the briefs, the senses and the career tools go through; every test
asserts the secret text never appears in anything a model was given.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import llm, privacy
from core.config import REPO_ROOT, settings
from tests import livedb

SECRET_SUBJECT = "SECRETSUBJECT-root canal for Jane Roe"
SECRET_BODY = "SECRETBODY Jane Roe DOB 01/02/1970 balance due"
SECRETS = ("SECRETSUBJECT", "SECRETBODY", "Jane Roe", "patient@client-dental.test", "01/02/1970")

RULES = """
rules:
  - label: dental clients
    domains: [client-dental.test]
    senders: [doctor@personal-mail.test]
    labels: [atlas-private]
    recipients_domains: [client-dental.test]
    calendar:
      attendee_domains: [client-dental.test]
      title_keywords: [patient]
    github:
      orgs: [client-org]
      repos: [someone/client-repo]
  - label: Oakline
    domains: [employer.test]
content_screen: true
mail_model_reads: {reads}
"""


@pytest.fixture
def rules(tmp_path, monkeypatch):
    """Rules in a temp file. Returns a setter for the two switches."""
    path = tmp_path / "model_exclude.yaml"

    def use(*, file_reads: bool = False, env_reads: bool = False) -> privacy.Rules:
        path.write_text(RULES.format(reads=str(file_reads).lower()), encoding="utf-8")
        monkeypatch.setattr(privacy, "RULES_PATH", path)
        monkeypatch.setattr(settings, "mail_model_reads", env_reads)
        privacy.reset_cache()
        return privacy.load()

    use()
    return use


class Spy:
    """Everything any model was handed in a test."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def text(self) -> str:
        return "\n".join(self.seen)

    def assert_clean(self) -> None:
        blob = self.text()
        for s in SECRETS:
            assert s not in blob, f"{s!r} reached a model"


@pytest.fixture
def spy(monkeypatch) -> Spy:
    s = Spy()

    async def complete(**kw: Any) -> str:
        s.seen.append(f"{kw.get('system', '')}\n{kw.get('user', '')}")
        return ""

    async def complete_json(**kw: Any) -> Any:
        s.seen.append(f"{kw.get('system', '')}\n{kw.get('user', '')}")
        if kw.get("purpose") == "triage":
            items = json.loads(kw.get("user") or "[]")
            return {"verdicts": [{"id": it["id"], "project": None, "urgency": 1, "importance": 1,
                                  "deadline": None, "gist": "visible", "ignore": False} for it in items]}
        return None

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr("core.runtime.agent.get_client", lambda: (_ for _ in ()).throw(AssertionError("real client")))
    return s


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(mid: str, sender: str, *, to: str = "me@gmail.test", cc: str = "", labels=("INBOX",),
         subject: str = SECRET_SUBJECT, body: str = SECRET_BODY) -> dict[str, Any]:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "snippet": body[:80],
            "internalDate": str(int(datetime(2026, 10, 20, 14, 0, tzinfo=UTC).timestamp() * 1000)),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(body)}}}


# ------------------------------------------------------------------ matching


def test_exclusion_beats_the_known_people_list(rules, monkeypatch):
    from core import context_pack
    from core.connectors import gmail

    pack = {"people": [{"name": "Dr Doe", "emails": ["patient@client-dental.test"], "tier": 1, "project": "dental"}]}
    monkeypatch.setattr(context_pack, "load", lambda *a, **k: pack)
    sig = gmail.message_to_signal(_msg("m1", "Dr Doe <patient@client-dental.test>"))
    assert sig is not None and sig.private and sig.private_rule == "dental clients"
    assert sig.subject == "Private message (dental clients)" and sig.body == ""
    assert "known" not in sig.raw and "SECRET" not in json.dumps(sig.model_dump(mode="json"))


@pytest.mark.parametrize("sender,to,cc,labels,label", [
    ("a@client-dental.test", "me@x.test", "", (), "dental clients"),             # domain
    ("a@mail.client-dental.test", "me@x.test", "", (), "dental clients"),        # subdomain
    ("Doc <DOCTOR@personal-mail.test>", "me@x.test", "", (), "dental clients"),  # exact sender, any case
    ("me@x.test", "front@client-dental.test", "", (), "dental clients"),         # To
    ("me@x.test", "a@b.test", "Ops <ops@client-dental.test>", (), "dental clients"),  # Cc
    ("a@b.test", "me@x.test", "", ("Atlas-Private",), "dental clients"),         # label name, any case
])
def test_what_makes_mail_private(rules, sender, to, cc, labels, label):
    hit = privacy.is_private_mail(sender, recipients=[to, cc], labels=labels)
    assert hit is not None and hit.label == label


def test_ordinary_mail_and_lookalike_domains_stay_visible(rules):
    assert privacy.is_private_mail("a@notclient-dental.test", recipients=["me@x.test"]) is None
    assert privacy.is_private_mail("a@client-dental.test.evil", recipients=[]) is None
    assert privacy.is_private_mail("a@b.test", labels=["INBOX", "IMPORTANT"]) is None


def test_gmail_resolves_label_ids_to_names_and_fails_closed_without_them(rules):
    from core.connectors import gmail

    msg = _msg("m2", "a@b.test", labels=("INBOX", "Label_42"))
    assert gmail.message_to_signal(msg, {"Label_42": "atlas-private", "INBOX": "INBOX"}).private
    assert not gmail.message_to_signal(msg, {"Label_42": "receipts", "INBOX": "INBOX"}).private
    unknown = gmail.message_to_signal(msg, None)  # names unreadable while label rules exist
    assert unknown.private and unknown.private_rule == gmail.UNCHECKED_LABEL


def test_calendar_attendee_and_title_rules(rules):
    from core.connectors import gcal

    base = {"id": "e1", "status": "confirmed", "summary": "Weekly sync", "updated": "2026-10-20T12:00:00Z",
            "start": {"dateTime": "2026-10-22T15:00:00-05:00"}, "end": {"dateTime": "2026-10-22T16:00:00-05:00"},
            "attendees": [{"email": "x@y.test"}], "organizer": {"email": "me@x.test"},
            "description": SECRET_BODY}
    assert not gcal.event_to_signal(base).private
    by_attendee = gcal.event_to_signal({**base, "attendees": [{"email": "dr@client-dental.test"}]})
    by_title = gcal.event_to_signal({**base, "summary": "Patient consult"})
    assert by_attendee.private and by_attendee.private_rule == "dental clients"
    assert by_title.private


def test_github_org_and_repo_rules(rules):
    from core.connectors import github

    run = {"id": 1, "name": "CI", "conclusion": "failure", "event": "push", "run_attempt": 1,
           "head_branch": "main", "html_url": "https://gh/run/1", "updated_at": "2026-10-20T12:00:00Z"}
    assert github.run_to_signal(run, "client-org/anything").private
    assert github.run_to_signal(run, "someone/client-repo").private
    assert not github.run_to_signal(run, "trilok/atlas").private


# ------------------------------------------------------------------ the content screen


@pytest.mark.parametrize("text", [
    "SSN 123-45-6789 on file",
    "Please confirm the DOB for the claim",
    "Date of birth: March 3",
    "MRN 448812",
    "her medical record number is attached",
    "Patient: Jane Roe needs a follow up",
    "Jane Roe, a patient of Dr. Smith",
    "Member ID: XZ1234",
    "insurance # on the card",
])
def test_the_screen_catches_patient_data(rules, text):
    hit = privacy.screen(text)
    assert hit is not None and hit.label == privacy.SCREEN_LABEL


@pytest.mark.parametrize("text", [
    "Your order #123-456 has shipped",
    "Meeting with John Smith on Friday",
    "Thanks for your patience with the deploy",
    "Be patient, the build is slow",
    "Invoice 2026-10 attached",
    "Call me at 832-555-0100",
])
def test_the_screen_leaves_ordinary_mail_alone(rules, text):
    assert privacy.screen(text) is None


def test_the_screen_can_be_turned_off(rules, tmp_path, monkeypatch):
    path = tmp_path / "off.yaml"
    path.write_text("content_screen: false\n", encoding="utf-8")
    assert privacy.screen("DOB 01/02/1970", rules=privacy.load(path)) is None


# ------------------------------------------------------------------ storage


async def test_storage_keeps_no_body_and_a_placeholder(rules, monkeypatch):
    from core.signals import Signal, get_signal, insert_signal

    async with livedb.shadow(monkeypatch, "signals"):
        when = datetime(2026, 10, 20, tzinfo=UTC)
        private = await insert_signal(Signal(source="gmail", external_id="p1", occurred_at=when, subject=SECRET_SUBJECT,
                                             body=SECRET_BODY, actor="Jane", private=True, private_rule="Oakline"))
        screened = await insert_signal(Signal(source="gmail", external_id="s1", occurred_at=when,
                                              subject="Claim", body="Member ID: 77 for the claim"))
        safe_start = await insert_signal(Signal(source="gmail", external_id="v1", occurred_at=when,
                                                subject="Lunch?", body="ordinary text"))
        canvas = await insert_signal(Signal(source="canvas", external_id="c1", occurred_at=when,
                                            subject="Essay due", body="Write 500 words"))
        p, s, v, c = [await get_signal(i) for i in (private, screened, safe_start, canvas)]
    assert (p["subject"], p["body"], p["private"], p["private_rule"]) == ("Private message (Oakline)", "", True, "Oakline")
    assert s["private"] and s["private_rule"] == privacy.SCREEN_LABEL and s["body"] == ""
    assert not v["private"] and v["body"] == "" and v["subject"] == "Lunch?", "safe-start: sender and subject only"
    assert c["body"] == "Write 500 words", "Canvas is not gated"


# ------------------------------------------------------------------ every model path

TABLES = ("signals", "deadlines", "triggers", "projects", "system_heartbeats", "llm_calls", "episodes")


async def _seed(monkeypatch) -> None:
    """A private row with its content still in it (as if written before the gate),
    a safe-start Gmail row, and a visible Canvas row."""
    from core import db

    now = datetime.now(UTC) - timedelta(minutes=5)
    rows = [("gmail", "p1", "Jane", SECRET_SUBJECT, SECRET_BODY, True, "dental clients",
             {"from": "patient@client-dental.test"}),
            ("gmail", "g1", "Jane", SECRET_SUBJECT + " (gmail)", SECRET_BODY, False, None,
             {"from": "patient@client-dental.test"}),
            ("canvas", "c1", "Prof", "Essay due Friday", "Write 500 words", False, None, {})]
    for src, ext, actor, subject, body, private, rule, raw in rows:
        await db.execute(
            "insert into signals (source, external_id, occurred_at, actor, subject, body, raw, private, private_rule) "
            "values ($1, $2, $3, $4, $5, $6, $7, $8, $9)", src, ext, now, actor, subject, body, raw, private, rule)


async def test_triage_scores_hidden_signals_by_rules_and_never_sends_them(rules, spy, monkeypatch):
    from core import db
    from core.loop.triage import triage_new_signals

    async with livedb.shadow(monkeypatch, *TABLES):
        await _seed(monkeypatch)
        out = await triage_new_signals()
        rows = {r["external_id"]: dict(r) for r in await db.fetch("select * from signals")}
    spy.assert_clean()
    assert out["by_rules"] == 2 and "Essay due Friday" in spy.text(), "the visible Canvas row still went"
    assert rows["p1"]["triage_notes"].startswith("Scored by rules: private (dental clients)")
    assert rows["g1"]["triage_notes"] == "Scored by rules: mail model reads are off."
    assert rows["p1"]["urgency"] == 1 and rows["p1"]["importance"] == 1


async def test_prepare_tasks_never_get_a_hidden_signal(rules, spy, monkeypatch):
    from core import db
    from core.loop import senses

    handed: list[str] = []

    async def dispatch(agent: str, task: str, **_: Any) -> Any:
        handed.append(task)
        return SimpleNamespace(text="ok")

    monkeypatch.setattr(senses, "dispatch", dispatch)
    async with livedb.shadow(monkeypatch, *TABLES, "actions"):
        await _seed(monkeypatch)
        await db.execute("update signals set triage_status = 'triaged', urgency = 2")
        out = await senses.route_signals(lambda text: None)  # type: ignore[arg-type,return-value]
    spy.seen += handed
    spy.assert_clean()
    assert out["prepared"] == 1 and "Essay due Friday" in handed[0]


async def test_briefs_count_hidden_mail_by_label_and_show_the_model_none_of_it(rules, spy, monkeypatch):
    from core import db
    from core.loop import briefs

    given: list[str] = []
    delivered: list[str] = []

    async def dispatch(label: str, prompt: str, context: str, by: str) -> Any:
        given.append(f"{prompt}\n{context}")
        return SimpleNamespace(text="Your evening, written by the model.")

    async def deliver(text: str, kind: str, *, store: bool = True) -> None:
        delivered.append(text)

    async def blank() -> str:
        return ""

    monkeypatch.setattr(briefs, "_dispatch_with_retry", dispatch)
    monkeypatch.setattr(briefs, "_deliver", deliver)
    monkeypatch.setattr(briefs, "daily_health_line", blank)
    monkeypatch.setattr(briefs, "daily_cost_line", blank)
    monkeypatch.setattr(briefs, "_owes", lambda: "")
    monkeypatch.setattr(settings, "brief_skip_unchanged", False)
    async with livedb.shadow(monkeypatch, *TABLES):
        await _seed(monkeypatch)
        await db.execute("update signals set triage_status = 'triaged'")
        await briefs.run_brief("evening")
        line = await briefs._private("evening")
    spy.seen += given
    spy.assert_clean()
    assert "Essay due Friday" in given[0]
    assert line == ("Private mail Atlas did not read: dental clients 1. "
                    "1 new mail, calendar or GitHub signal, not yet read by a model (safe-start).")
    assert delivered and delivered[0].rstrip().endswith(line), "appended by code after the model's text"
    for s in SECRETS:
        assert s not in delivered[0]


async def test_the_quiet_brief_carries_the_line_and_a_private_arrival_counts_as_a_change(rules, monkeypatch):
    from core.loop import briefs

    async with livedb.shadow(monkeypatch, *TABLES, "approvals", "goals", "night_tasks", "job_events", "recordings"):
        before = datetime.now(UTC) - timedelta(hours=1)
        await _seed(monkeypatch)
        from core import db

        await db.execute("update signals set triage_status = 'triaged'")
        changes = await briefs.changes_since(before)
        text = await briefs.quiet_brief_text("morning", before, datetime.now(UTC), ([], {}))
    assert changes["signals"] == 3, "private arrivals are a change: no quiet brief over them"
    assert "Private mail Atlas did not read: dental clients 1." in text
    for s in SECRETS:
        assert s not in text


async def test_knowledge_graph_extraction_never_gets_hidden_content(rules, spy, monkeypatch):
    """Worst case: the CEO echoes its whole brief context, and the extractor reads it."""
    from core import db
    from core.knowledge_graph import link_text
    from core.loop import briefs

    async with livedb.shadow(monkeypatch, *TABLES, "entities", "relationships"):
        await _seed(monkeypatch)
        await db.execute("update signals set triage_status = 'triaged'")
        context = await briefs._signals_context("evening")
        await link_text(context + "\nThe brief, at length, so the extractor has something to read.")
    spy.assert_clean()


async def test_the_job_mail_scan_skips_private_mail_and_asks_no_model_in_safe_start(rules, spy, monkeypatch):
    from core.jobs import mail as jobs_mail

    msgs = {"j1": _msg("j1", "Recruiting <no-reply@client-dental.test>", subject="Your application", body="Thanks"),
            "j2": _msg("j2", "Talent <talent@acme.test>", subject="Your application to Acme",
                       body="Patient: Jane Roe applied"),
            "j3": _msg("j3", "Talent <talent@greenhouse.io>", subject="Thank you for applying", body="We got it")}

    class Conn:
        async def label_names(self) -> dict[str, str]:
            return {}

        async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "/messages":
                return {"messages": [{"id": k} for k in msgs]}
            return msgs[path.rsplit("/", 1)[-1]]

    async with livedb.shadow(monkeypatch, "job_applications", "job_events", "job_postings"):
        out = await jobs_mail.scan(Conn())
    assert out["private"] == 2, "the excluded domain and the patient-data snippet are never filed"
    spy.assert_clean()
    assert not [s for s in spy.seen if "jobs_mail" in s or "job application" in s.lower()], \
        "safe-start: no model reads job mail"


def test_mail_learning_drops_excluded_threads_and_asks_no_model_in_safe_start(rules, spy):
    import asyncio

    from core import mail_learning as ml

    now = datetime.now(UTC)
    me = "me@x.test"
    threads = [
        ml.Thread("t1", [ml.Message(f"Me <{me}>", to="Dr <patient@client-dental.test>", subject=SECRET_SUBJECT,
                                    at=now - timedelta(days=2)),
                         ml.Message("Dr <patient@client-dental.test>", to=me, subject=SECRET_SUBJECT,
                                    at=now - timedelta(days=1))]),
        ml.Thread("t2", [ml.Message(f"Me <{me}>", to="Pal <pal@friend.test>", subject="Dinner",
                                    at=now - timedelta(days=2)),
                         ml.Message("Pal <pal@friend.test>", to=me, subject="Dinner", at=now - timedelta(days=1))]),
    ]
    result = ml.analyze(me, threads, [], now=now)
    blob = json.dumps(result, default=str)
    for s in SECRETS:
        assert s not in blob
    assert "pal@friend.test" in blob
    people = [{"emails": ["pal@friend.test"], "name": "Pal", "tier": 1, "subjects": ["Dinner"]}]
    asyncio.run(ml._label_projects(people))
    assert spy.seen == [], "the labelling step is a model call: off in safe-start"


def test_open_loops_hide_excluded_and_count_mail_loops_for_the_model(rules):
    from core import context_pack

    data = {"snapshot": date.today().isoformat(), "open_loops": {"dental": [
        {"owner": "me", "who": "Dr", "addr": "patient@client-dental.test", "since": "2026-10-01",
         "text": SECRET_SUBJECT, "mail": True},
        {"owner": "me", "who": "Pal", "addr": "pal@friend.test", "since": "2026-10-01", "text": "Dinner plans",
         "mail": True},
        {"owner": "me", "who": "Landlord", "since": "2026-10-01", "text": "Lease renewal"}]}}
    block = context_pack.open_loops_block(data)
    assert SECRET_SUBJECT not in block and "Dinner plans" not in block and "Lease renewal" in block
    assert "1 more open email thread, not read by a model" in block
    owes = context_pack.owes_block(data)
    assert SECRET_SUBJECT not in owes and "Dinner plans" in owes, "owes goes to Trilok, not a model"


async def test_mcp_tools_return_placeholders_only(rules, monkeypatch):
    from core import db
    from core.mcp_server import recent_signals

    async with livedb.shadow(monkeypatch, *TABLES):
        await _seed(monkeypatch)
        await db.execute("update signals set triage_status = 'triaged'")
        out = await recent_signals(hours=24)
    blob = json.dumps(out, default=str)
    for s in SECRETS:
        assert s not in blob
    gists = sorted(r["gist"] for r in out)
    assert "Private message (dental clients)" in gists and "gmail signal (not read by a model)" in gists
    assert any(r["source"] == "canvas" and r["gist"] == "Essay due Friday" for r in out)


async def test_consolidation_packs_and_the_ceos_agenda_skip_hidden_content(rules, monkeypatch):
    from core import db, planner
    from core.memory import consolidation
    from core.projects import Project

    async with livedb.shadow(monkeypatch, *TABLES):
        await _seed(monkeypatch)
        pid = await db.fetchval("insert into projects (slug, name, kind, status, priority) "
                                "values ('dental', 'Dental', 'client', 'active', 2) returning id")
        await db.execute("update signals set project_id = $1", pid)
        project = Project(**dict(await db.fetchrow("select * from projects where id = $1", pid)))
        monkeypatch.setattr(consolidation.retrieval, "search", _no_search)
        material = await consolidation._pack_material(project)
    for s in SECRETS:
        assert s not in material
    plan = {"days": [{"items": [{"title": "Dentist board call", "source": "gcal", "private": False, "url": "u"},
                                {"title": "Essay", "source": "canvas", "private": False}]}]}
    masked = planner.for_model(plan)["days"][0]["items"]
    assert masked[0]["title"] == "gcal signal (not read by a model)" and masked[0]["url"] is None
    assert masked[1]["title"] == "Essay"


async def _no_search(*_: Any, **__: Any) -> list[Any]:
    return []


async def test_the_career_agent_sees_no_mail_text_in_safe_start(rules, monkeypatch):
    from agents.career.tools import build_tools
    from core.jobs import store

    tool = {t.name: t for t in build_tools()}["get_application"]
    async with livedb.shadow(monkeypatch, "job_applications", "job_events"):
        app = await store.upsert_application("Acme", "AI Engineer", status="applied")
        await store.record_event(app["id"], "invite", f"{SECRET_SUBJECT} | {SECRET_BODY}", message_id="m-1")
        await store.record_event(app["id"], "note", "Called the recruiter")
        out = await tool.handler({"id": str(app["id"])})
    details = [e["detail"] for e in out["events"]]
    assert "(from mail, not read by a model)" in details and "Called the recruiter" in details
    for s in SECRETS:
        assert s not in json.dumps(out, default=str)


# ------------------------------------------------------------------ safe-start and the flip


def test_safe_start_needs_both_switches_and_never_shows_a_private_signal(rules):
    gmail_row = {"source": "gmail", "private": False}
    private_row = {"source": "gmail", "private": True, "private_rule": "Oakline"}
    canvas_row = {"source": "canvas", "private": False}
    for file_reads, env_reads in ((False, False), (True, False), (False, True)):
        rules(file_reads=file_reads, env_reads=env_reads)
        assert not privacy.model_visible(gmail_row) and privacy.model_visible(canvas_row)
    rules(file_reads=True, env_reads=True)
    assert privacy.model_visible(gmail_row) and privacy.model_visible({"source": "github", "private": False})
    assert not privacy.model_visible(private_row), "the flip never reveals a private signal"


def test_a_missing_or_broken_rules_file_keeps_model_reads_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mail_model_reads", True)
    monkeypatch.setattr(privacy, "RULES_PATH", tmp_path / "missing.yaml")
    privacy.reset_cache()
    assert not privacy.mail_model_reads()
    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [unclosed\nmail_model_reads: true\n", encoding="utf-8")
    monkeypatch.setattr(privacy, "RULES_PATH", broken)
    privacy.reset_cache()
    assert not privacy.mail_model_reads()


def test_the_brief_line_has_counts_and_labels_only(rules):
    rows = [{"source": "gmail", "private": True, "private_rule": "Oakline", "subject": SECRET_SUBJECT,
             "actor": "patient@client-dental.test"}] * 2 + [
        {"source": "gmail", "private": True, "private_rule": "dental clients"},
        {"source": "gcal", "private": False, "subject": SECRET_SUBJECT},
        {"source": "canvas", "private": False}]
    line = privacy.brief_line(rows)
    assert line == ("Private mail Atlas did not read: Oakline 2, dental clients 1. "
                    "1 new mail, calendar or GitHub signal, not yet read by a model (safe-start).")
    assert not re.search(r"@|SECRET", line)
    assert privacy.brief_line([{"source": "canvas"}]) == ""


# ------------------------------------------------------------------ scopes and approvals


def test_google_scopes_are_read_only():
    from core import connect
    from scripts import google_auth

    for scopes in (connect.GOOGLE_SCOPES, google_auth.SCOPES):
        assert sorted(scopes) == ["https://www.googleapis.com/auth/calendar.readonly",
                                  "https://www.googleapis.com/auth/gmail.readonly"]


async def test_a_write_attempt_fails_with_a_clear_message():
    import httpx

    from core.connectors.base import ConnectorError
    from core.connectors.gmail import GmailConnector

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "insufficient scope"}})

    async def token() -> str:
        return "t"

    conn = GmailConnector(token_provider=token, client_factory=lambda: httpx.AsyncClient(
        base_url="https://gmail.test", transport=httpx.MockTransport(handler)))
    with pytest.raises(ConnectorError, match="connected read-only"):
        await conn.create_draft(to="a@b.test", subject="s", body="b")


def _policy_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for f in sorted((REPO_ROOT / "db" / "migrations").glob("*.sql")):
        rows += re.findall(r"\('(\*[a-z_*]+\*?)',\s*'(auto|approve|deny)'", f.read_text(encoding="utf-8"))
    return rows


# Verbs that reach someone outside Atlas, matched against whole words of the
# tool name (dismiss_posting is a local note, not a post).
_OUTWARD = {"send", "draft", "email", "post", "publish", "submit", "reply", "comment", "invite", "share",
            "upload", "delete", "transfer", "pay", "payment", "tweet", "message", "event"}


def test_every_outward_tool_needs_an_approval_card():
    """Sweep: a tool under agents/*/tools.py whose name has an outward verb must
    match an approve or deny row. An unmatched tool is auto, which is the gap."""
    import fnmatch

    rows = _policy_rows()
    names = set()
    for f in (REPO_ROOT / "agents").glob("*/tools.py"):
        names |= set(re.findall(r'name="([a-z_]+)"', f.read_text(encoding="utf-8")))
    outward = sorted(n for n in names if _OUTWARD & set(n.split("_")))
    assert "draft_email" in outward and "calendar_create_event" in outward
    for n in outward:
        gates = [g for pat, g in rows if fnmatch.fnmatch(n, pat)]
        assert gates and all(g in ("approve", "deny") for g in gates), f"{n} has no approval row"


async def test_draft_email_needs_an_approval_card(monkeypatch):
    from core.runtime import policy

    async with livedb.shadow(monkeypatch, "approval_policy"):
        from core import db

        for f in ("011_senses.sql", "035_privacy_gate.sql"):
            sql = (REPO_ROOT / "db" / "migrations" / f).read_text(encoding="utf-8")
            for stmt in re.findall(r"insert into approval_policy.*?;", sql, flags=re.S):
                await db.execute(stmt)
        assert (await policy.gate_for("draft_email"))[0] == "approve"


async def test_migration_035_is_idempotent_on_the_shadowed_live_tables(monkeypatch):
    from core import db

    sql = (REPO_ROOT / "db" / "migrations" / "035_privacy_gate.sql").read_text(encoding="utf-8")
    async with livedb.shadow(monkeypatch, "signals", "approval_policy"):
        await db.execute(sql)
        await db.execute(sql)
        cols = {r["column_name"] for r in await db.fetch(
            "select column_name from information_schema.columns where table_name = 'signals' "
            "and column_name in ('private', 'private_rule')")}
    assert cols == {"private", "private_rule"}


# ------------------------------------------------------------------ the preview script


def test_the_preview_prints_domains_and_labels_never_addresses_or_subjects(rules, tmp_path, capsys):
    from scripts import exclusion_preview

    fixture = tmp_path / "inbox.json"
    fixture.write_text(json.dumps({"label_names": {"Label_1": "atlas-private"}, "messages": [
        _msg("1", "Dr <patient@client-dental.test>"),
        _msg("2", "Pal <pal@friend.test>", subject="Dinner", body="see you"),
        _msg("3", "Pal <pal@friend.test>", labels=("INBOX", "Label_1"), subject="x", body="y"),
        _msg("4", "Clinic <a@b.test>", subject="Claim", body="DOB 01/02/1970"),
    ]}), encoding="utf-8")
    assert exclusion_preview.main(["--fixture", str(fixture)]) == 0
    out = capsys.readouterr().out
    assert "client-dental.test  dental clients" in out and "friend.test" in out and "visible" in out
    assert "possible patient data" in out and "dental clients: 2" in out
    assert "@" not in out and "SECRET" not in out and "Dinner" not in out and "Jane" not in out
    assert "OFF (safe-start)" in out


def test_the_preview_never_runs_against_gmail_in_tests():
    src = (Path(__file__).read_text(encoding="utf-8"))
    assert src.count("from_gmail(") == 1, "only mentioned here; the tests use --fixture"
