"""Tests for core/connectors/canvas.py: pure mapping plus a mocked sync
against the live test database. No Canvas call is ever made."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from core import projects, signals
from core.connectors import state
from core.connectors.canvas import (
    CanvasConnector,
    _next_link,
    announcement_signal,
    assignment_signals,
    strip_html,
)
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # Real Canvas signals, the real cursor and real projects stay untouched.
    async with livedb.shadow(monkeypatch, "signals", "connector_state", "projects"):
        yield


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assignment(aid: int, due_in_days: int | None, **sub) -> dict:
    due = None if due_in_days is None else _iso(datetime.now(UTC) + timedelta(days=due_in_days))
    return {
        "id": aid, "name": f"Lab {aid}", "due_at": due, "points_possible": 20,
        "updated_at": _iso(datetime.now(UTC)), "html_url": f"https://c/a/{aid}",
        "description": "<p>Write the <b>report</b>.</p>",
        "submission": sub or {"workflow_state": "unsubmitted"},
    }


async def test_strip_html_and_next_link():
    assert strip_html("<p>Write the <b>report</b>.</p>\n\n  now") == "Write the report . now"
    assert strip_html(None) == ""
    link = '<https://c/api/v1/x?page=2>; rel="next", <https://c/api/v1/x?page=5>; rel="last"'
    assert _next_link(link) == "https://c/api/v1/x?page=2"
    assert _next_link('<https://c/x?page=1>; rel="first"') is None
    assert _next_link(None) is None


async def test_assignment_mapping_due_window_submission_and_grade():
    upcoming = assignment_signals(_assignment(1, 5), "ITAI-2373")
    assert len(upcoming) == 1
    s = upcoming[0]
    assert s.subject.startswith("ITAI-2373: Lab 1 due ")
    assert s.deadline == date.fromisoformat(s.subject.rsplit(" ", 1)[1])
    assert s.body.startswith("20 points. Write the report")
    assert s.external_id.startswith("assignment:1:")

    # A moved due date is a different external id, so it surfaces again.
    moved = assignment_signals(_assignment(1, 9), "ITAI-2373")[0]
    assert moved.external_id != s.external_id

    # Already submitted: no deadline nag.
    assert assignment_signals(_assignment(2, 5, workflow_state="submitted"), "C") == []
    # Long past, or far future, or no due date: nothing.
    assert assignment_signals(_assignment(3, -30), "C") == []
    assert assignment_signals(_assignment(4, 200), "C") == []
    assert assignment_signals(_assignment(5, None), "C") == []

    # Graded: one grade signal (and no deadline signal since it is graded).
    graded_at = _iso(datetime.now(UTC) - timedelta(hours=2))
    out = assignment_signals(
        _assignment(6, 2, workflow_state="graded", score=18.0, graded_at=graded_at), "C"
    )
    assert len(out) == 1
    assert out[0].subject == "C: graded Lab 6: 18/20"
    assert out[0].external_id.startswith("grade:6:")
    assert out[0].deadline is None


async def test_announcement_mapping():
    s = announcement_signal({
        "id": 77, "title": "Exam moved", "message": "<p>Exam is <i>Friday</i></p>",
        "posted_at": "2026-10-06T15:00:00Z", "html_url": "https://c/ann/77",
        "author": {"display_name": "Dr. Lee"},
    }, "ITAI-2373")
    assert s.subject == "ITAI-2373 announcement: Exam moved"
    assert s.body == "Exam is Friday"
    assert s.actor == "Dr. Lee"
    assert s.external_id == "announcement:77"


def _transport(calls: list[httpx.Request], assignments: list[dict], anns: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        p = request.url.path
        if p == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 11, "name": "AI Apps", "course_code": "ITAI-2373"}])
        if p == "/api/v1/courses/11/assignments":
            if "page=2" in str(request.url):
                return httpx.Response(200, json=assignments[1:])
            return httpx.Response(
                200, json=assignments[:1],
                headers={"Link": f'<{request.url}&page=2>; rel="next"'} if len(assignments) > 1 else {},
            )
        if p == "/api/v1/announcements":
            return httpx.Response(200, json=anns)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _connector(transport) -> CanvasConnector:
    return CanvasConnector(
        base_url="https://hccs.instructure.com", token="tok",
        client_factory=lambda: httpx.AsyncClient(base_url="https://hccs.instructure.com", transport=transport),
    )


async def test_sync_links_college_project_follows_pages_and_is_idempotent():
    college = await projects.upsert_project("test-hcc", "HCC", kind="college")
    calls: list[httpx.Request] = []
    assignments = [_assignment(1, 4), _assignment(2, 6)]
    anns = [{"id": 5, "title": "Welcome", "message": "hi", "posted_at": "2026-10-01T00:00:00Z"},
            {"id": 9, "title": "Exam", "message": "soon", "posted_at": "2026-10-06T00:00:00Z"}]
    c = _connector(_transport(calls, assignments, anns))

    out = await c.sync()
    assert out == {"courses": 1, "new_signals": 4, "failures": []}
    assert calls[0].headers["Authorization"] == "Bearer tok"
    assert any("page=2" in str(r.url) for r in calls)  # pagination followed
    rows = await signals.list_signals("new", limit=100)
    mine = [r for r in rows if r["source"] == "canvas"]
    assert len(mine) == 4
    assert all(r["project_id"] == college.id for r in mine)
    dated = [r for r in mine if r["deadline"]]
    assert len(dated) == 2
    assert (await state.get_cursor("canvas")) == {"announcement": 9}

    # Nothing new on the second pass.
    assert (await c.sync())["new_signals"] == 0


async def test_sync_skips_when_unconfigured_and_records_course_failure():
    calls: list[httpx.Request] = []
    off = CanvasConnector(base_url="", token="", client_factory=lambda: httpx.AsyncClient(
        transport=_transport(calls, [], [])))
    assert (await off.sync())["courses"] == 0 and calls == []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 11, "course_code": "ITAI-2373"}])
        return httpx.Response(401)

    c = CanvasConnector(base_url="https://x", token="bad", client_factory=lambda: httpx.AsyncClient(
        base_url="https://x", transport=httpx.MockTransport(handler)))
    out = await c.sync()
    assert out["failures"] == ["ITAI-2373"]
    s = await state.get_state("canvas")
    assert s["consecutive_failures"] == 1 and "401" in (s["last_error"] or "")
