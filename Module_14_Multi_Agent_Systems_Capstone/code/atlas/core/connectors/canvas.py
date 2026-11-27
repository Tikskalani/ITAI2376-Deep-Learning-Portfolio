"""Atlas -- Canvas LMS connector (HCC Eagle Online).

Reads active courses, their assignments with the owner's submission
state, and course announcements through the Canvas REST API with a
personal access token. Produces three kinds of signal:

  assignment   an assignment with a due date Atlas has not seen at that
               due date (a moved due date is a new signal), deadline set
  graded       a submission that has a new grade
  announcement an instructor announcement

Everything is idempotent on external_id, so the connector simply reads
the current state every tick and lets the unique key drop what it has
already seen. The only cursor is the newest announcement id.

Canvas pagination is a Link header; assignments per course rarely exceed
one page of 100 but the reader follows `rel="next"` up to a small limit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from core.config import settings
from core.connectors import state
from core.connectors.base import Connector, ConnectorError, TransientConnectorError
from core.projects import list_projects
from core.signals import Signal, insert_signal

log = logging.getLogger(__name__)

SOURCE = "canvas"
_TIMEOUT = httpx.Timeout(20.0)
_MAX_PAGES = 5
# Assignments due more than this far back are history, not signals.
_LOOKBACK_DAYS = 3
_LOOKAHEAD_DAYS = 90

ClientFactory = Callable[[], httpx.AsyncClient]
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_date(when: datetime) -> date:
    return when.astimezone(ZoneInfo(settings.timezone)).date()


def assignment_signals(
    assignment: dict[str, Any], course_code: str, project_id: Any = None
) -> list[Signal]:
    """Zero, one or two signals for an assignment: posted/moved, and graded."""
    out: list[Signal] = []
    aid = assignment.get("id")
    name = assignment.get("name") or "assignment"
    due = _parse_ts(assignment.get("due_at"))
    now = datetime.now(UTC)
    url = assignment.get("html_url")
    points = assignment.get("points_possible")
    sub = assignment.get("submission") or {}

    if due is not None:
        in_window = (
            now - timedelta(days=_LOOKBACK_DAYS) <= due <= now + timedelta(days=_LOOKAHEAD_DAYS)
        )
        submitted = sub.get("workflow_state") in ("submitted", "graded", "pending_review")
        if in_window and not submitted:
            due_local = _local_date(due)
            body = strip_html(assignment.get("description"))[:2000]
            if points is not None:
                body = f"{points} points. {body}".strip()
            out.append(
                Signal(
                    source=SOURCE,
                    external_id=f"assignment:{aid}:{due.isoformat()}",
                    occurred_at=_parse_ts(assignment.get("updated_at")) or now,
                    actor=course_code,
                    subject=f"{course_code}: {name} due {due_local.isoformat()}",
                    body=body,
                    url=url,
                    project_id=project_id,
                    deadline=due_local,
                    raw={"type": "assignment", "assignment_id": aid, "course": course_code},
                )
            )

    graded_at = _parse_ts(sub.get("graded_at"))
    if graded_at is not None and sub.get("score") is not None:
        score = sub.get("score")
        total = f"/{points:g}" if isinstance(points, (int, float)) else ""
        out.append(
            Signal(
                source=SOURCE,
                external_id=f"grade:{aid}:{graded_at.isoformat()}",
                occurred_at=graded_at,
                actor=course_code,
                subject=f"{course_code}: graded {name}: {score:g}{total}",
                body=strip_html(sub.get("grade_comments") or "")[:500] if isinstance(sub.get("grade_comments"), str) else "",
                url=sub.get("preview_url") or url,
                project_id=project_id,
                raw={"type": "graded", "assignment_id": aid, "course": course_code,
                     "score": score, "points_possible": points},
            )
        )
    return out


def announcement_signal(
    ann: dict[str, Any], course_code: str, project_id: Any = None
) -> Signal:
    posted = _parse_ts(ann.get("posted_at")) or datetime.now(UTC)
    return Signal(
        source=SOURCE,
        external_id=f"announcement:{ann.get('id')}",
        occurred_at=posted,
        actor=(ann.get("author") or {}).get("display_name") or course_code,
        subject=f"{course_code} announcement: {ann.get('title')}",
        body=strip_html(ann.get("message"))[:2000],
        url=ann.get("html_url"),
        project_id=project_id,
        raw={"type": "announcement", "course": course_code, "announcement_id": ann.get("id")},
    )


class CanvasConnector(Connector):
    """Polls courses, assignments (with submissions) and announcements."""

    name = SOURCE
    cache_ttl_seconds = 0.0
    max_attempts = 3

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self.base_url = (base_url if base_url is not None else settings.canvas_url).rstrip("/")
        self.token = token if token is not None else settings.canvas_token
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=_TIMEOUT)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "User-Agent": "atlas-senses"}

    async def _fetch_fresh(self, key: str) -> Any:
        """key is a path with query string; follows Link rel=next pages."""
        items: list[Any] = []
        url: str | None = key
        async with self._client_factory() as client:
            for _ in range(_MAX_PAGES):
                if not url:
                    break
                try:
                    resp = await client.get(url, headers=self._headers())
                except httpx.HTTPError as exc:
                    raise TransientConnectorError(f"canvas {key}: {exc}") from exc
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise TransientConnectorError(f"canvas {key}: HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise ConnectorError(f"canvas {key}: HTTP {resp.status_code}")
                data = resp.json()
                if isinstance(data, list):
                    items.extend(data)
                else:
                    return data
                url = _next_link(resp.headers.get("link"))
        return items

    async def _college_project_id(self) -> Any:
        for p in await list_projects("active"):
            if p.kind == "college":
                return p.id
        return None

    async def sync(self) -> dict[str, Any]:
        """Read every active course into signals. Never raises."""
        summary: dict[str, Any] = {"courses": 0, "new_signals": 0, "failures": []}
        if not self.token or not self.base_url:
            log.info("canvas connector: not configured; skipping")
            return summary
        pid = await self._college_project_id()
        try:
            courses = (await self.get(
                "/api/v1/courses?enrollment_state=active&per_page=50", allow_stale=False
            )).data or []
        except Exception as exc:
            log.warning("canvas connector: course list failed: %s", exc)
            await state.record_failure(SOURCE, f"courses: {exc}")
            summary["failures"].append("courses")
            return summary

        cursor = await state.get_cursor(SOURCE)
        last_ann = int(cursor.get("announcement", 0))
        newest_ann = last_ann
        any_ok = False
        for course in courses:
            cid = course.get("id")
            code = course.get("course_code") or course.get("name") or f"course {cid}"
            summary["courses"] += 1
            try:
                assignments = (await self.get(
                    f"/api/v1/courses/{cid}/assignments?per_page=100&include[]=submission"
                    "&order_by=due_at",
                    allow_stale=False,
                )).data or []
                for a in assignments:
                    for sig in assignment_signals(a, code, pid):
                        if await insert_signal(sig) is not None:
                            summary["new_signals"] += 1
                anns = (await self.get(
                    f"/api/v1/announcements?context_codes[]=course_{cid}&per_page=20",
                    allow_stale=False,
                )).data or []
                for ann in anns:
                    try:
                        aid = int(ann.get("id"))
                    except (TypeError, ValueError):
                        continue
                    newest_ann = max(newest_ann, aid)
                    if last_ann and aid <= last_ann:
                        continue
                    if await insert_signal(announcement_signal(ann, code, pid)) is not None:
                        summary["new_signals"] += 1
            except Exception as exc:
                log.warning("canvas connector: course %s failed: %s", code, exc)
                summary["failures"].append(code)
                await state.record_failure(SOURCE, f"{code}: {exc}")
                continue
            any_ok = True
        if any_ok or not courses:
            await state.set_cursor(SOURCE, {"announcement": newest_ann})
        return summary


def _next_link(link_header: str | None) -> str | None:
    """The rel="next" URL from a Canvas Link header, or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segs = [s.strip() for s in part.split(";")]
        if len(segs) >= 2 and segs[1] == 'rel="next"':
            return segs[0].strip("<>")
    return None


__all__ = [
    "CanvasConnector",
    "assignment_signals",
    "announcement_signal",
    "strip_html",
    "SOURCE",
]
