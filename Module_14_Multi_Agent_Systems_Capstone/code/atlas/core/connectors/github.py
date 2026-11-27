"""Atlas -- GitHub connector.

Reads what happened on the client repos and turns it into signals:
pushes, pull requests opened, merged, reviewed or waiting on review,
issues and comments, releases, and failed Actions runs. Two endpoints per
repo, both cheap and both cursor-based so a tick that finds nothing new
costs one request each:

  GET /repos/{owner}/{repo}/events          newest first, ids increase
  GET /repos/{owner}/{repo}/actions/runs    newest first, status=failure

The cursor (connector_state, source "github") holds the last event id and
the last failed-run id per repo. Repos come from ATLAS_GITHUB_REPOS plus
the `repos` column of every active project; a project's repo links its
signals to the project without any model call.

The connector never raises out of sync(): per-repo failures are recorded
in connector_state and the next repo is tried. 404 and 401 are permanent
(wrong name, missing scope); 5xx, 429 and timeouts are transient and
retried by the base class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from core import privacy
from core.config import settings
from core.connectors import state
from core.connectors.base import Connector, ConnectorError, TransientConnectorError
from core.projects import list_projects, project_for_repo
from core.signals import Signal, insert_signal

log = logging.getLogger(__name__)

API = "https://api.github.com"
SOURCE = "github"
_TIMEOUT = httpx.Timeout(20.0)

# Event types the connector turns into signals; everything else (watch,
# fork, member, create/delete branch) is noise for a single-owner repo.
_INTERESTING = {
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "ReleaseEvent",
}

ClientFactory = Callable[[], httpx.AsyncClient]


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_to_signal(event: dict[str, Any], repo: str, project_id: Any = None) -> Signal | None:
    """Map one /events item to a Signal, or None when it is not interesting."""
    etype = event.get("type")
    if etype not in _INTERESTING:
        return None
    payload = event.get("payload") or {}
    actor = (event.get("actor") or {}).get("login")
    occurred = _parse_ts(event.get("created_at"))
    url: str | None = None
    body = ""

    if etype == "PushEvent":
        commits = payload.get("commits") or []
        ref = (payload.get("ref") or "").removeprefix("refs/heads/")
        n = len(commits) or payload.get("size") or 0
        subject = f"{actor} pushed {n} commit{'s' if n != 1 else ''} to {repo}@{ref}"
        body = "\n".join(
            f"- {(c.get('message') or '').splitlines()[0][:200]}" for c in commits[:20]
        )
        before, head = payload.get("before"), payload.get("head")
        if before and head:
            url = f"https://github.com/{repo}/compare/{before[:12]}...{head[:12]}"
    elif etype == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        action = payload.get("action") or "updated"
        if action == "closed" and pr.get("merged"):
            action = "merged"
        if action == "review_requested":
            reviewer = (payload.get("requested_reviewer") or {}).get("login")
            action = f"requested review from {reviewer} on" if reviewer else "requested review on"
        subject = f"{actor} {action} PR #{pr.get('number')}: {pr.get('title')}"
        body = (pr.get("body") or "")[:2000]
        url = pr.get("html_url")
    elif etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        verdict = (review.get("state") or "commented").replace("_", " ")
        subject = f"{actor} reviewed PR #{pr.get('number')} ({verdict}): {pr.get('title')}"
        body = (review.get("body") or "")[:2000]
        url = review.get("html_url") or pr.get("html_url")
    elif etype == "PullRequestReviewCommentEvent":
        pr = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        subject = f"{actor} commented on PR #{pr.get('number')} code: {pr.get('title')}"
        body = (comment.get("body") or "")[:2000]
        url = comment.get("html_url")
    elif etype == "IssuesEvent":
        issue = payload.get("issue") or {}
        subject = f"{actor} {payload.get('action')} issue #{issue.get('number')}: {issue.get('title')}"
        body = (issue.get("body") or "")[:2000]
        url = issue.get("html_url")
    elif etype == "IssueCommentEvent":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        kind = "PR" if issue.get("pull_request") else "issue"
        subject = f"{actor} commented on {kind} #{issue.get('number')}: {issue.get('title')}"
        body = (comment.get("body") or "")[:2000]
        url = comment.get("html_url")
    else:  # ReleaseEvent
        rel = payload.get("release") or {}
        subject = f"{actor} published release {rel.get('tag_name')} of {repo}"
        body = (rel.get("body") or "")[:2000]
        url = rel.get("html_url")

    hit = privacy.is_private_repo(repo)  # a client's repo never reaches a model
    return Signal(
        source=SOURCE,
        private=hit is not None,
        private_rule=hit.label if hit else None,
        external_id=f"{repo}:event:{event.get('id')}",
        occurred_at=occurred,
        actor=actor,
        subject=subject,
        body=body,
        url=url,
        project_id=project_id,
        raw={"type": etype, "repo": repo, "action": payload.get("action")},
    )


def run_to_signal(run: dict[str, Any], repo: str, project_id: Any = None) -> Signal:
    """A failed Actions run as a signal. Failed CI is urgent by nature."""
    occurred = _parse_ts(run.get("updated_at") or run.get("created_at"))
    hit = privacy.is_private_repo(repo)
    return Signal(
        source=SOURCE,
        private=hit is not None,
        private_rule=hit.label if hit else None,
        external_id=f"{repo}:run:{run.get('id')}",
        occurred_at=occurred,
        actor=(run.get("actor") or {}).get("login"),
        subject=(
            f"CI failed: {run.get('name') or 'workflow'} on {repo}@"
            f"{run.get('head_branch')} ({(run.get('display_title') or '')[:80]})"
        ),
        body=f"conclusion={run.get('conclusion')} event={run.get('event')} attempt={run.get('run_attempt')}",
        url=run.get("html_url"),
        project_id=project_id,
        raw={"type": "WorkflowRunFailed", "repo": repo, "conclusion": run.get("conclusion")},
    )


class GitHubConnector(Connector):
    """Polls the events and failed-runs endpoints for each configured repo."""

    name = SOURCE
    cache_ttl_seconds = 0.0  # sync always wants fresh data
    max_attempts = 3

    def __init__(
        self,
        token: str | None = None,
        repos: list[str] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self.token = token if token is not None else settings.github_token
        self.extra_repos = repos if repos is not None else settings.github_repo_list
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=API, timeout=_TIMEOUT)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "atlas-senses",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _fetch_fresh(self, key: str) -> Any:
        """key is 'events:owner/repo' or 'runs:owner/repo'."""
        kind, _, repo = key.partition(":")
        params: dict[str, int | str]
        if kind == "events":
            path, params = f"/repos/{repo}/events", {"per_page": 100}
        elif kind == "runs":
            path, params = f"/repos/{repo}/actions/runs", {"per_page": 20, "status": "failure"}
        else:
            raise ConnectorError(f"github: unknown key {key!r}")
        async with self._client_factory() as client:
            try:
                resp = await client.get(path, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                raise TransientConnectorError(f"github {key}: {exc}") from exc
        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientConnectorError(f"github {key}: HTTP {resp.status_code}")
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise TransientConnectorError(f"github {key}: rate limited")
        if resp.status_code >= 400:
            raise ConnectorError(f"github {key}: HTTP {resp.status_code}")
        data = resp.json()
        return data.get("workflow_runs", []) if kind == "runs" else data

    async def repos(self) -> list[str]:
        """Configured repos plus every active project's repos, deduplicated."""
        seen: dict[str, None] = {}
        for r in self.extra_repos:
            seen.setdefault(r.strip().lower(), None)
        for p in await list_projects("active"):
            for r in p.repos:
                seen.setdefault(r, None)
        return list(seen)

    async def sync(self) -> dict[str, Any]:
        """Pull new events and failed runs for every repo into signals.

        Returns {repos, new_signals, failures}. Never raises.
        """
        summary: dict[str, Any] = {"repos": 0, "new_signals": 0, "failures": []}
        if not self.token:
            log.info("github connector: no token configured; skipping")
            return summary
        repos = await self.repos()
        if not repos:
            log.info("github connector: no repos configured; skipping")
            return summary
        cursor = await state.get_cursor(SOURCE)
        per_repo: dict[str, dict[str, int]] = dict(cursor.get("repos") or {})
        any_ok = False
        for repo in repos:
            summary["repos"] += 1
            rc = dict(per_repo.get(repo) or {})
            try:
                new = await self._sync_repo(repo, rc)
            except Exception as exc:
                log.warning("github connector: %s failed: %s", repo, exc)
                summary["failures"].append(repo)
                await state.record_failure(SOURCE, f"{repo}: {exc}")
                continue
            any_ok = True
            summary["new_signals"] += new
            per_repo[repo] = rc
        if any_ok:
            await state.set_cursor(SOURCE, {"repos": per_repo})
        return summary

    async def _sync_repo(self, repo: str, rc: dict[str, int]) -> int:
        """Sync one repo, mutating rc (its cursor). Returns new signal count."""
        project = await project_for_repo(repo)
        pid = project.id if project else None
        new = 0

        events = (await self.get(f"events:{repo}", allow_stale=False)).data or []
        last_event = int(rc.get("event", 0))
        newest_event = last_event
        for ev in events:
            try:
                eid = int(ev.get("id"))
            except (TypeError, ValueError):
                continue
            newest_event = max(newest_event, eid)
            if last_event and eid <= last_event:
                continue
            sig = event_to_signal(ev, repo, pid)
            if sig is None:
                continue
            # First sync of a repo: record history but do not flood triage
            # with weeks of old events.
            if not last_event and _older_than_days(sig.occurred_at, 7):
                continue
            if await insert_signal(sig) is not None:
                new += 1
        rc["event"] = newest_event

        runs = (await self.get(f"runs:{repo}", allow_stale=False)).data or []
        last_run = int(rc.get("run", 0))
        newest_run = last_run
        for run in runs:
            try:
                rid = int(run.get("id"))
            except (TypeError, ValueError):
                continue
            newest_run = max(newest_run, rid)
            if last_run and rid <= last_run:
                continue
            sig = run_to_signal(run, repo, pid)
            if not last_run and _older_than_days(sig.occurred_at, 7):
                continue
            if await insert_signal(sig) is not None:
                new += 1
        rc["run"] = newest_run
        return new


def _older_than_days(when: datetime, days: int) -> bool:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).days >= days


__all__ = ["GitHubConnector", "event_to_signal", "run_to_signal", "SOURCE"]
