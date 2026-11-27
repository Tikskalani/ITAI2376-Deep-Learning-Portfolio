"""Record → transcript → notes (core/recordings.py and its routes).

faster-whisper and the agent are faked; the audio lands in a temp folder;
the recordings table is a private copy (tests/livedb.py).
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from core import db, recordings
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")

NOTES = """# Lecture 4: Gradient descent

## Main ideas
- The loss surface and why we follow the negative gradient.
- Learning rate: too big diverges, too small crawls.

## Action items
- Read chapter 3 before Thursday (due 2026-10-16)
- [ ] Try the notebook with three learning rates
- none of the above is optional
"""


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _private(monkeypatch, tmp_path):
    monkeypatch.setattr(recordings, "DIR", tmp_path / "recordings")
    async with livedb.shadow(monkeypatch, "recordings", "episodes", "notifications", "projects", "deadlines", "triggers"):
        yield
        # A background transcription that outlives the test would write to the
        # real tables once the private copies are gone. Wait for every one.
        if recordings._tasks:
            await asyncio.gather(*list(recordings._tasks), return_exceptions=True)


def _fake_dispatch(text: str = NOTES):
    calls: list[tuple[str, str]] = []

    async def dispatch(agent, task, *, dispatched_by, participants):
        calls.append((agent, task))
        ep = await db.fetchval(
            "insert into episodes (started_at, ended_at, agent_name, summary) values (now(), now(), $1, 'notes') returning id",
            agent,
        )
        return SimpleNamespace(text=text, episode_id=ep, usage={})

    dispatch.calls = calls  # type: ignore[attr-defined]
    return dispatch


def test_action_items_parse_with_and_without_dates():
    items = recordings.parse_action_items(NOTES)
    assert items == [
        {"text": "Read chapter 3 before Thursday", "due": "2026-10-16"},
        {"text": "Try the notebook with three learning rates", "due": None},
        {"text": "none of the above is optional", "due": None},
    ]
    assert recordings.parse_action_items("## Action items\n- none\n") == []
    assert recordings.parse_action_items("no section here") == []


@aio
async def test_create_stores_the_audio_and_checks_the_input(tmp_path):
    rec = await recordings.create(b"RIFF....", filename="Lecture.m4a", kind="lecture", title="  Lecture 4 ")
    assert rec["status"] == "uploaded" and rec["title"] == "Lecture 4" and rec["bytes"] == 8
    assert recordings.audio_path(rec) is not None and recordings.audio_path(rec).read_bytes() == b"RIFF...."
    untitled = await recordings.create(b"x", filename="a.webm", kind="meeting")
    assert untitled["title"].startswith("Meeting ")
    for bad, msg in ((dict(filename="notes.txt"), "unsupported"), (dict(filename="a.webm", kind="song"), "kind"),
                     (dict(filename="a.webm", project="nope"), "no project")):
        with pytest.raises(ValueError, match=msg):
            await recordings.create(b"x", **bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        await recordings.create(b"", filename="a.webm")
    # The path check never serves a file from outside the recordings folder.
    assert recordings.audio_path({"path": str(tmp_path / "elsewhere.webm")}) is None


@aio
async def test_process_transcribes_then_writes_notes_and_tells_him(monkeypatch):
    async def fake_transcribe(path):
        return "we follow the negative gradient. read chapter three before thursday.", 3720

    monkeypatch.setattr(recordings, "_transcribe", fake_transcribe)
    rec = await recordings.create(b"audio", filename="l.webm", kind="lecture", title="Lecture 4")
    fake = _fake_dispatch()
    out = await recordings.process(rec["id"], dispatch_fn=fake)
    assert out["status"] == "done" and out["duration_s"] == 3720 and out["episode_id"]
    assert out["transcript"].startswith("we follow") and out["notes"] == NOTES.strip()
    assert [i["due"] for i in out["action_items"]] == ["2026-10-16", None, None]
    agent, task = fake.calls[0]
    assert agent == "academic" and "Transcript of a lecture" in task and "## Action items" in task
    assert "we follow the negative gradient" in task
    n = await db.fetchrow("select title, data from notifications where kind = 'recording'")
    assert n["title"] == "Notes ready: Lecture 4" and n["data"]["recording_id"] == str(rec["id"])
    # The action items are on the Planner, dated where the notes dated them,
    # and processing the same recording again adds nothing twice.
    tasks = await db.fetch("select title, due_date, source, source_ref from deadlines order by created_at")
    assert [(t["title"], t["due_date"]) for t in tasks] == [
        ("Read chapter 3 before Thursday", date(2026, 10, 16)),
        ("Try the notebook with three learning rates", None),
        ("None of the above is optional", None),
    ]
    assert {t["source"] for t in tasks} == {"recording"} and {t["source_ref"] for t in tasks} == {str(rec["id"])}
    await recordings.process(rec["id"], dispatch_fn=_fake_dispatch())
    assert await db.fetchval("select count(*) from deadlines") == 3

    # A meeting goes to the COO; a note to the CEO.
    for kind, agent_name in (("meeting", "coo"), ("note", "ceo")):
        r = await recordings.create(b"audio", filename="m.webm", kind=kind)
        fake = _fake_dispatch("## Action items\n- none")
        out = await recordings.process(r["id"], dispatch_fn=fake)
        assert fake.calls[0][0] == agent_name and out["action_items"] == []


@aio
async def test_failures_land_on_the_row_and_retry_works(monkeypatch):
    async def silent(path):
        return None, None

    monkeypatch.setattr(recordings, "_transcribe", silent)
    rec = await recordings.create(b"audio", filename="l.webm")
    out = await recordings.process(rec["id"], dispatch_fn=_fake_dispatch())
    assert out["status"] == "failed" and "transcription produced no text" in out["error"]

    async def ok(path):
        return "words", 10

    monkeypatch.setattr(recordings, "_transcribe", ok)
    out = await recordings.process(rec["id"], dispatch_fn=_fake_dispatch(""))
    assert out["status"] == "failed" and "no notes" in out["error"]
    out = await recordings.process(rec["id"], dispatch_fn=_fake_dispatch())
    assert out["status"] == "done" and out["error"] is None
    assert await recordings.process(uuid4()) is None


@aio
async def test_routes_upload_list_detail_audio_and_retry(monkeypatch):
    async def ok(path):
        return "words", 10

    monkeypatch.setattr(recordings, "_transcribe", ok)
    monkeypatch.setattr("core.runtime.dispatcher.dispatch", _fake_dispatch())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.post("/api/recordings", files={"file": ("standup.webm", b"audio", "audio/webm")},
                         data={"kind": "meeting", "title": "Standup"})
        assert r.status_code == 200 and r.json()["recording"]["status"] == "uploaded"
        rid = r.json()["recording"]["id"]
        assert (await c.post("/api/recordings", files={"file": ("x.txt", b"hi", "text/plain")})).status_code == 422
        for _ in range(50):  # the background task finishes within a moment
            await asyncio.sleep(0.1)
            if (await c.get(f"/api/recordings/{rid}")).json()["recording"]["status"] == "done":
                break
        detail = (await c.get(f"/api/recordings/{rid}")).json()["recording"]
        assert detail["status"] == "done" and detail["notes"] == NOTES.strip() and detail["transcript"] == "words"
        listed = (await c.get("/api/recordings")).json()["recordings"]
        assert [x["id"] for x in listed][:1] == [rid] and "transcript" not in listed[0]
        audio = await c.get(f"/api/recordings/{rid}/audio")
        assert audio.status_code == 200 and audio.content == b"audio"
        assert (await c.get(f"/api/recordings/{uuid4()}")).status_code == 404
        assert (await c.get(f"/api/recordings/{uuid4()}/audio")).status_code == 404
        assert (await c.post(f"/api/recordings/{rid}/process")).json() == {"started": True}
        assert (await c.post(f"/api/recordings/{uuid4()}/process")).status_code == 404
        await asyncio.sleep(0.3)
