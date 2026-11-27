"""Tests for core/connectors/github.py.

Event mapping is pure. sync() runs against the live test database with
httpx routed through a MockTransport, so no GitHub call is ever made.
Signals, projects and the connector cursor are private copies for each
test (tests/livedb.py), so real repos are never synced and nothing
written here outlives the test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from core import projects, signals
from core.connectors import state
from core.connectors.github import GitHubConnector, event_to_signal, run_to_signal
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")

REPO = "test-owner/portal"


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup(monkeypatch):
    # The connector syncs every active project's repos and keeps one cursor
    # for all of them; real projects and the real cursor stay out of reach.
    async with livedb.shadow(monkeypatch, "signals", "connector_state", "projects"):
        yield


def _now_iso(minutes_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _push(eid: str, minutes_ago: int = 1) -> dict:
    return {
        "id": eid, "type": "PushEvent", "created_at": _now_iso(minutes_ago),
        "actor": {"login": "trilok"},
        "payload": {
            "ref": "refs/heads/main", "before": "a" * 40, "head": "b" * 40,
            "commits": [{"message": "Fix invoice rounding\n\nlong body"}, {"message": "Bump deps"}],
        },
    }


def _pr(eid: str, action: str, merged: bool = False, minutes_ago: int = 1) -> dict:
    return {
        "id": eid, "type": "PullRequestEvent", "created_at": _now_iso(minutes_ago),
        "actor": {"login": "michael"},
        "payload": {
            "action": action,
            "pull_request": {"number": 7, "title": "Add implant pricing", "body": "see spec",
                             "merged": merged, "html_url": f"https://github.com/{REPO}/pull/7"},
            "requested_reviewer": {"login": "trilok"} if action == "review_requested" else None,
        },
    }


async def test_event_mapping_push_pr_review_issue_release_and_noise():
    s = event_to_signal(_push("1"), REPO)
    assert s is not None
    assert s.subject == f"trilok pushed 2 commits to {REPO}@main"
    assert "- Fix invoice rounding" in s.body and "long body" not in s.body
    assert s.url == f"https://github.com/{REPO}/compare/aaaaaaaaaaaa...bbbbbbbbbbbb"
    assert s.external_id == f"{REPO}:event:1"

    merged = event_to_signal(_pr("2", "closed", merged=True), REPO)
    assert merged is not None and merged.subject == "michael merged PR #7: Add implant pricing"
    rr = event_to_signal(_pr("3", "review_requested"), REPO)
    assert rr is not None and rr.subject.startswith("michael requested review from trilok on PR #7")

    review = event_to_signal({
        "id": "4", "type": "PullRequestReviewEvent", "created_at": _now_iso(),
        "actor": {"login": "trilok"},
        "payload": {"review": {"state": "changes_requested", "body": "nit", "html_url": "u"},
                    "pull_request": {"number": 7, "title": "Add implant pricing"}},
    }, REPO)
    assert review is not None and "(changes requested)" in review.subject

    issue = event_to_signal({
        "id": "5", "type": "IssuesEvent", "created_at": _now_iso(),
        "actor": {"login": "client"},
        "payload": {"action": "opened", "issue": {"number": 9, "title": "Login broken", "body": "", "html_url": "i"}},
    }, REPO)
    assert issue is not None and issue.subject == "client opened issue #9: Login broken"

    comment = event_to_signal({
        "id": "6", "type": "IssueCommentEvent", "created_at": _now_iso(),
        "actor": {"login": "client"},
        "payload": {"issue": {"number": 7, "title": "Add implant pricing", "pull_request": {"url": "https://api/pr/7"}},
                    "comment": {"body": "LGTM?", "html_url": "c"}},
    }, REPO)
    assert comment is not None and comment.subject == "client commented on PR #7: Add implant pricing"

    rel = event_to_signal({
        "id": "7", "type": "ReleaseEvent", "created_at": _now_iso(),
        "actor": {"login": "trilok"},
        "payload": {"release": {"tag_name": "v1.2", "body": "", "html_url": "r"}},
    }, REPO)
    assert rel is not None and rel.subject == f"trilok published release v1.2 of {REPO}"

    assert event_to_signal({"id": "8", "type": "WatchEvent", "payload": {}}, REPO) is None
    assert event_to_signal({"id": "9", "type": "CreateEvent", "payload": {}}, REPO) is None


async def test_failed_run_mapping():
    s = run_to_signal({
        "id": 555, "name": "deploy", "head_branch": "main", "display_title": "Fix invoice rounding",
        "conclusion": "failure", "event": "push", "run_attempt": 1,
        "updated_at": _now_iso(), "actor": {"login": "trilok"},
        "html_url": f"https://github.com/{REPO}/actions/runs/555",
    }, REPO)
    assert s.subject == f"CI failed: deploy on {REPO}@main (Fix invoice rounding)"
    assert s.external_id == f"{REPO}:run:555"
    assert s.raw["type"] == "WorkflowRunFailed"


def _transport(events: list[dict], runs: list[dict], calls: list[httpx.Request],
               status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": runs})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _connector(transport: httpx.MockTransport, repos: list[str] | None = None) -> GitHubConnector:
    return GitHubConnector(
        token="test-token",
        repos=repos if repos is not None else [],
        client_factory=lambda: httpx.AsyncClient(
            base_url="https://api.github.com", transport=transport,
        ),
    )


async def test_sync_inserts_new_signals_links_project_and_advances_cursor():
    proj = await projects.upsert_project("test-portal", "Portal", repos=[REPO])
    calls: list[httpx.Request] = []
    old = {**_push("90", minutes_ago=60 * 24 * 30)}  # a month old: history, skipped
    events = [_pr("102", "opened"), _push("101"), old]
    runs = [{"id": 7, "name": "ci", "head_branch": "main", "display_title": "x",
             "conclusion": "failure", "event": "push", "run_attempt": 1,
             "updated_at": _now_iso(), "actor": {"login": "t"}, "html_url": "u"}]
    c = _connector(_transport(events, runs, calls))

    out = await c.sync()
    assert out == {"repos": 1, "new_signals": 3, "failures": []}
    assert calls[0].headers["Authorization"] == "Bearer test-token"
    assert "per_page=100" in str(calls[0].url)
    rows = await signals.list_signals("new", limit=100)
    mine = [r for r in rows if r["external_id"].startswith(REPO)]
    assert {r["external_id"] for r in mine} == {
        f"{REPO}:event:101", f"{REPO}:event:102", f"{REPO}:run:7",
    }
    assert all(r["project_id"] == proj.id for r in mine)
    cursor = await state.get_cursor("github")
    assert cursor == {"repos": {REPO: {"event": 102, "run": 7}}}
    again = await projects.get_project("test-portal")
    assert again is not None and again.last_signal_at is not None

    # Second sync: same payload, nothing new, cursor unchanged, no duplicates.
    out = await c.sync()
    assert out["new_signals"] == 0
    assert len([r for r in await signals.list_signals("new", limit=100)
                if r["external_id"].startswith(REPO)]) == 3

    # Third sync with one newer event: only that one lands.
    events.insert(0, _push("103"))
    out = await c.sync()
    assert out["new_signals"] == 1
    assert (await state.get_cursor("github"))["repos"][REPO]["event"] == 103


async def test_sync_records_failure_and_keeps_cursor():
    await state.set_cursor("github", {"repos": {REPO: {"event": 50, "run": 1}}})
    calls: list[httpx.Request] = []
    c = _connector(_transport([], [], calls, status=404), repos=[REPO])
    c.max_attempts = 1
    out = await c.sync()
    assert out["failures"] == [REPO] and out["new_signals"] == 0
    s = await state.get_state("github")
    assert s["consecutive_failures"] == 1
    assert "404" in (s["last_error"] or "")
    assert s["cursor"] == {"repos": {REPO: {"event": 50, "run": 1}}}


async def test_sync_skips_without_token_or_repos():
    calls: list[httpx.Request] = []
    no_token = GitHubConnector(token="", repos=[REPO],
                               client_factory=lambda: httpx.AsyncClient(transport=_transport([], [], calls)))
    assert (await no_token.sync())["repos"] == 0
    no_repos = _connector(_transport([], [], calls), repos=[])
    assert (await no_repos.sync())["repos"] == 0
    assert calls == []


async def test_repos_merges_config_and_active_projects():
    await projects.upsert_project("test-a", "A", repos=["Test-Owner/A"])
    await projects.upsert_project("test-b", "B", repos=["test-owner/b"], status="parked")
    c = _connector(_transport([], [], []), repos=["test-owner/a", "test-owner/c"])
    assert await c.repos() == ["test-owner/a", "test-owner/c"]


async def test_transient_errors_are_retried_then_recorded(monkeypatch):
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(503)

    c = GitHubConnector(token="t", repos=[REPO], client_factory=lambda: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    c.max_attempts = 2
    monkeypatch.setattr("core.connectors.base.wait_exponential", lambda **kw: (lambda _: 0))
    out = await c.sync()
    assert out["failures"] == [REPO]
    assert seen["n"] == 2  # retried once, then gave up
    assert json.loads(json.dumps(out))  # summary is JSON-safe for the heartbeat
