"""Candidate answers (core/jobs/answers.py and /api/jobs/answers).

Pure rules need nothing. The store tests run on a private, empty copy of
candidate_answers (tests/livedb.py), so Trilok's real answers on the laptop
are never read, changed or rewritten by a test run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from core.jobs import answers
from core.web.api import app
from tests import livedb


@pytest_asyncio.fixture(loop_scope="module")
async def _private(monkeypatch):
    # Deleting and re-inserting real rows put their content back but not their
    # timestamps; a private copy leaves the live table alone.
    async with livedb.shadow(monkeypatch, "candidate_answers"):
        yield


def db_test(fn):
    """A database test: runs on the module loop on a private candidate_answers."""
    return pytest.mark.usefixtures("_private")(pytest.mark.asyncio(loop_scope="module")(fn))


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


# ------------------------------------------------------------------ pure


@pytest.mark.parametrize("text,key", [
    ("Do you have 3+ years of Python experience? *", "q:do you have 3 years of python experience"),
    ("  DO YOU HAVE 3+ years of python experience (required)", "q:do you have 3 years of python experience"),
    ("Portfolio link (optional)", "q:portfolio link"),
    ("", ""),
    ("?!", ""),
])
def test_question_key_normalizes(text, key):
    assert answers.question_key(text) == key


def test_standard_wording_maps_to_standard_keys():
    assert answers.standard_for("Are you legally authorized to work in the United States? *").key == "work_authorized_us"
    assert answers.standard_for("Will you now or in the future require sponsorship for employment visa status?").key \
        == "needs_sponsorship"
    assert answers.standard_for("Email").key == "email"
    assert answers.standard_for("Have you shipped an LLM product?") is None


@pytest.mark.parametrize("key,value,ok", [
    ("email", "trilok@example.com", True),
    ("email", "not an email", False),
    ("phone", "+1 (713) 555-0100", True),
    ("phone", "call me", False),
    ("linkedin_url", "https://www.linkedin.com/in/someone", True),
    ("linkedin_url", "linkedin.com/in/someone", False),
    ("work_authorized_us", "Yes", True),
    ("work_authorized_us", "yes please", False),
    pytest.param("full_name", "x" * (answers.MAX_ANSWER + 1), False, id="too-long"),
    ("email", "", True),  # empty clears
    ("q:zz test anything", "free text is fine", True),
])
def test_validate(key, value, ok):
    if ok:
        assert answers.validate(key, value) == value.strip()
    else:
        with pytest.raises(answers.AnswerError):
            answers.validate(key, value)


def test_every_standard_question_is_well_formed():
    keys = [q.key for q in answers.STANDARD]
    assert len(keys) == len(set(keys))
    for q in answers.STANDARD:
        assert q.kind in ("text", "email", "phone", "url", "choice")
        assert (q.kind == "choice") == bool(q.choices)
        assert q.default is None or q.default in q.choices
    eeo = [q for q in answers.STANDARD if q.group.startswith("Voluntary")]
    assert eeo and all(q.default == answers.DECLINE and not q.required for q in eeo)


# ------------------------------------------------------------------ store


@db_test
async def test_standard_answer_saves_clears_and_counts_toward_required():
    await answers.set_answer("full_name", "")
    before = await answers.overview()
    assert "full_name" in before["missing_required"]

    row = await answers.set_answer("full_name", "  Trilok Test  ")
    assert row["answer"] == "Trilok Test" and row["standard"] and row["answered_at"] is not None
    after = await answers.overview()
    assert "full_name" not in after["missing_required"]
    assert next(s for s in after["standard"] if s["key"] == "full_name")["answer"] == "Trilok Test"

    cleared = await answers.set_answer("full_name", "")
    assert cleared["answer"] is None and cleared["answered_at"] is None
    assert "full_name" in (await answers.overview())["missing_required"]


@db_test
async def test_invalid_standard_answer_is_refused_and_not_saved():
    await answers.set_answer("email", "")
    with pytest.raises(answers.AnswerError):
        await answers.set_answer("email", "nope")
    assert await answers.answer_for("Email") is None


@db_test
async def test_eeo_defaults_to_decline_until_answered():
    await answers.set_answer("gender", "")
    assert await answers.answer_for("Gender") == answers.DECLINE
    assert (await answers.form_profile())["gender"] == answers.DECLINE
    await answers.set_answer("gender", "Male")
    assert await answers.answer_for("Gender") == "Male"


@db_test
async def test_standard_wording_on_a_form_reads_the_standard_answer():
    await answers.set_answer("work_authorized_us", "Yes")
    assert await answers.answer_for("Are you legally authorized to work in the United States?") == "Yes"
    # Standard wording is never recorded as a learned question.
    assert await answers.ask("Are you legally authorized to work in the United States?") == "work_authorized_us"
    assert not [p for p in (await answers.overview())["pending"] if "authorized" in p["question"].lower()]


@db_test
async def test_ask_records_once_notifies_once_then_answer_is_reused():
    q = "ZZ test: have you deployed an agent to production?"
    with patch("core.surfaces.notify.notify", new=AsyncMock()) as sent:
        key = await answers.ask(q, {"company": "Testco"})
        again = await answers.ask(q + " *", {"company": "Otherco"})
    assert key == again == answers.question_key(q)
    assert sent.await_count == 1
    _kind, title, body = sent.await_args.args
    assert "Testco" in body and q in body and "answer" in title.lower()

    pending = next(p for p in (await answers.overview())["pending"] if p["key"] == key)
    assert pending["asked_count"] == 2 and pending["context"] == {"company": "Otherco"}
    assert await answers.answer_for(q) is None

    await answers.set_answer(key, "Yes, Atlas runs on my laptop in production.")
    assert await answers.answer_for(q.upper() + "?") == "Yes, Atlas runs on my laptop in production."
    ov = await answers.overview()
    assert key not in [p["key"] for p in ov["pending"]]
    assert key in [lrn["key"] for lrn in ov["learned"]]
    assert (await answers.form_profile())[key].startswith("Yes")

    # Asking an answered question neither notifies nor un-answers it.
    with patch("core.surfaces.notify.notify", new=AsyncMock()) as sent:
        await answers.ask(q)
    assert sent.await_count == 0
    assert await answers.answer_for(q) is not None

    # Clearing it puts it back to waiting.
    await answers.set_answer(key, "")
    assert key in [p["key"] for p in (await answers.overview())["pending"]]


@db_test
async def test_learned_answer_needs_a_known_question_or_its_text():
    with pytest.raises(answers.AnswerError):
        await answers.set_answer("q:zz test never asked", "something")
    with pytest.raises(answers.AnswerError):
        await answers.set_answer("not_a_key", "something")
    row = await answers.set_answer("q:zz test given with text", "Five years", question="ZZ test given with text")
    assert row["question"] == "ZZ test given with text" and not row["standard"]


@db_test
async def test_forget_learned_but_not_standard():
    key = answers.question_key("ZZ test forget me")
    await answers.set_answer(key, "x", question="ZZ test forget me")
    assert await answers.forget(key) is True
    assert await answers.forget(key) is False
    with pytest.raises(answers.AnswerError):
        await answers.forget("email")


@db_test
async def test_ask_refuses_an_empty_question():
    with pytest.raises(answers.AnswerError):
        await answers.ask("  ?  ")


# ------------------------------------------------------------------ api


@db_test
async def test_api_lists_saves_validates_and_forgets(client):
    r = await client.get("/api/jobs/answers")
    assert r.status_code == 200
    body = r.json()
    assert {"standard", "learned", "pending", "missing_required"} <= body.keys()
    assert [s["key"] for s in body["standard"]] == [q.key for q in answers.STANDARD]

    r = await client.post("/api/jobs/answers", json={"key": "email", "answer": "bad"})
    assert r.status_code == 400 and "email" in r.json()["detail"].lower()
    r = await client.post("/api/jobs/answers", json={"key": "email", "answer": "t@example.com"})
    assert r.status_code == 200 and r.json()["answer"]["answer"] == "t@example.com"

    key = answers.question_key("ZZ test api question")
    r = await client.post("/api/jobs/answers", json={"key": key, "answer": "yes", "question": "ZZ test api question"})
    assert r.status_code == 200
    r = await client.post("/api/jobs/answers/forget", json={"key": key})
    assert r.status_code == 200 and r.json()["forgotten"] == key
    assert (await client.post("/api/jobs/answers/forget", json={"key": key})).status_code == 404
    assert (await client.post("/api/jobs/answers/forget", json={"key": "email"})).status_code == 400
