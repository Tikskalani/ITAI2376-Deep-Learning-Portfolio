"""The pipeline: capture, extract, resolve, claim, check, build. One run per
source at a time; every state is visible on the source row.

Cache policy (Trilok, 2026-10-20): the same link shared again, while its
result is done or running, reuses it and makes no paid call. A failed or
budget-held source stays as it is until he asks to try again (retry); a
done one is reprocessed only when he asks (refresh). Neither ever happens
silently, and each is its own attempt (cap_attempts.reason).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from core.capabilities import build, capture, extract, store
from core.capabilities.budget import HeldForBudget, SourceBudget
from core.config import settings

log = logging.getLogger(__name__)

CHUNK_SECONDS = 30.0
MAX_CHECKS = 2


class SubmitError(ValueError):
    """The link cannot be taken; the message says why."""


async def submit(url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Register a shared link. Returns {source, action}: action is "new",
    "reused" (done or running: nothing charged), "held" (failed or held:
    nothing charged until he retries) or "retry"/"refresh" (he asked).
    The caller runs process() for new, retry and refresh."""
    url = (url or "").strip()
    if not url.startswith(("https://", "http://")) or len(url) > 2000:
        raise SubmitError("share a full link starting with https://")
    key, kind = capture.canonical(url)
    existing = await store.get_source_by_canonical(key)
    if existing is None:
        return {"source": await store.create_source(url, key, kind, settings.cap_source_usd), "action": "new"}
    if existing["status"] == "processing":
        return {"source": existing, "action": "reused"}
    if not refresh:
        return {"source": existing, "action": "reused" if existing["status"] == "done" else "held"}
    return {"source": existing, "action": "refresh" if existing["status"] == "done" else "retry"}


async def process(source_id: str, reason: str = "first") -> str:
    """Run every stage for one source. Returns the final status: done,
    failed, held_budget, or "busy" when another run holds it."""
    src = await store.get_source(source_id)
    if src is None:
        return "failed"
    attempt = await store.claim_source(source_id, reason)
    if attempt is None:
        return "busy"
    # The cap is per source; a retry or refresh starts its own allowance.
    budget = SourceBudget(float(src["cap_usd"]))
    try:
        await _run(src, attempt, budget)
        await store.finish(source_id, attempt, "done")
        return "done"
    except HeldForBudget as held:
        await store.finish(source_id, attempt, "held_budget", f"held at {held.stage}: {held}")
        return "held_budget"
    except capture.CaptureError as exc:
        await store.finish(source_id, attempt, "failed", str(exc))
        return "failed"
    except Exception as exc:  # noqa: BLE001 - one failed source must not take the worker down
        log.exception("capabilities: %s failed", src["url"])
        await store.finish(source_id, attempt, "failed", f"{type(exc).__name__}: {str(exc)[:300]}")
        return "failed"
    finally:
        await store.add_cost(source_id, attempt, float(budget.spent))


def _ref(n: int) -> str:
    return f"E{n}"


async def _run(src: dict[str, Any], attempt: UUID, budget: SourceBudget) -> None:
    sid, n = src["id"], 0

    async def add(kind: str, **kw: Any) -> str:
        nonlocal n
        n += 1
        await store.add_evidence(sid, attempt, _ref(n), kind, **kw)
        return _ref(n)

    await store.set_stage(sid, attempt, "capture")
    if src["kind"] == "video":
        video = await capture.video(src["url"], sid)
        await store.set_meta(sid, video.title or None, video.author or None)
        if video.description.strip():
            await add("description", text=video.description[:8000])
        for ch in video.chapters[:30]:
            await add("description", text=f"Chapter: {ch.get('title', '')}", start_s=ch.get("start_time"))

        await store.set_stage(sid, attempt, "transcript")
        segments = await extract.transcript(video.path)
        for chunk in _chunks(segments):
            await add("transcript", text=chunk.text, start_s=chunk.start, end_s=chunk.end)

        await store.set_stage(sid, attempt, "frames")
        wanted = extract.frame_times(segments, video.duration, settings.cap_max_frames)
        frames = await extract.grab_frames(video.path, wanted, capture.source_dir(sid))
        # The frames and the transcript are the evidence; the video itself can be fetched again.
        video.path.unlink(missing_ok=True)
        readings = await extract.read_frames(frames, budget)
        by_index = {int(r.get("index", 0)): r for r in readings}
        for i, f in enumerate(frames, start=1):
            r = by_index.get(i)
            rel = _relative(f.file)
            if r is None:
                await add("frame", file=rel, start_s=f.t, extraction="unreadable", text="",
                          meta={"note": "the model returned no reading for this frame"})
                continue
            if not r.get("relevant"):
                continue
            parts = [p for p in (r.get("screen_text"), f"Code: {r['code']}" if r.get("code") else "",
                                 f"Action: {r['ui_action']}" if r.get("ui_action") else "") if p]
            await add("frame", file=rel, start_s=f.t, text="\n".join(parts)[:4000],
                      extraction=r.get("extraction") or "approximate", meta={"near": f.near})
    else:
        final, title, text = await capture.page(src["url"])
        await store.set_meta(sid, title or None, None)
        await add("resource", text=text[:20000], url=final)

    await store.set_stage(sid, attempt, "resources")
    items = await store.evidence(attempt)
    unavailable = False
    for url, pointed_by in build.referenced_urls(items)[:1]:
        resolved, error = await build.resolve_resource(url)
        if resolved:
            await add("resource", text=resolved, url=url, meta={"pointed_to_by": pointed_by})
        else:
            unavailable = True
            await add("resource", url=url, extraction="unavailable", text="",
                      meta={"pointed_to_by": pointed_by, "error": error})
    items = await store.evidence(attempt)

    await store.set_stage(sid, attempt, "claims")
    claims = await build.extract_claims(items, budget)
    claim_ids = []
    for c in claims:
        claim_ids.append(await store.add_claim(sid, attempt, c["text"], c["quote"], c["refs"], c["fidelity"],
                                               c["consequential"]))

    await store.set_stage(sid, attempt, "checks")
    resources = [e for e in items if e["kind"] == "resource" and e.get("text")]
    checked = 0
    for cid, c in zip(claim_ids, claims, strict=True):
        source_text, source_url, method = _check_source(c, resources)
        if checked < MAX_CHECKS and c["consequential"] and not source_text and c["primary_source"]:
            fetched, _err = await build.resolve_resource(c["primary_source"])
            source_text, source_url, method = fetched or "", c["primary_source"], "primary_source"
        if checked < MAX_CHECKS and c["consequential"] and source_text:
            verdict, excerpt = await build.check_claim(c, source_text, budget)
            await store.add_check(cid, verdict, method, source_url, excerpt)
            c.update(verdict=verdict, checked_url=source_url)
            checked += 1
        else:
            await store.add_check(cid, "not_checked", "none")
    if checked == 0:
        # At least one consequential claim gets a check when any source exists: the most consequential one.
        first = next(((cid, c) for cid, c in zip(claim_ids, claims, strict=True) if c["consequential"]), None)
        if first and resources:
            verdict, excerpt = await build.check_claim(first[1], resources[0]["text"], budget)
            await store.add_check(first[0], verdict, "resource", resources[0].get("url"), excerpt)
            first[1].update(verdict=verdict, checked_url=resources[0].get("url"))
            checked = 1

    await store.set_stage(sid, attempt, "procedure")
    body = await build.construct(items, claims, budget)
    body["source"] = {"url": src["url"], "title": (await store.get_source(sid) or {}).get("title")}
    title = body.get("title") or (await store.get_source(sid) or {}).get("title") or "Procedure"
    await store.new_version(title=title, source_id=sid, attempt_id=attempt, body=body,
                            check_status="partially_checked" if checked else "not_checked",
                            complete=not unavailable)
    await _announce(title)


def _check_source(claim: dict[str, Any], resources: list[dict[str, Any]]) -> tuple[str, str | None, str]:
    """The resolved resource, when the claim cites it."""
    for r in resources:
        if r["ref"] in claim["refs"]:
            return r["text"], r.get("url"), "resource"
    return "", None, "none"


class _Chunk:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start, self.end, self.text = start, end, text


def _chunks(segments: list[extract.Segment]) -> list[_Chunk]:
    """Transcript segments joined into about-30-second pieces: few enough to
    cite, small enough to point at a moment."""
    out: list[_Chunk] = []
    for s in segments:
        if out and s.end - out[-1].start <= CHUNK_SECONDS:
            out[-1].end, out[-1].text = s.end, out[-1].text + " " + s.text
        else:
            out.append(_Chunk(s.start, s.end, s.text))
    return out


def _relative(path: Path) -> str:
    try:
        return path.relative_to(settings.capabilities_dir).as_posix()
    except ValueError:
        return path.name


async def _announce(title: str) -> None:
    try:
        from core.surfaces.notify import notify

        await notify("capability", f"A procedure is ready for review: {title}",
                     "Atlas read the tutorial, checked what it could, and wrote a procedure. "
                     "Review it in Inbox, Skills to review, before any agent can use it.",
                     data={"view": "learn"})
    except Exception:  # noqa: BLE001 - the version is saved; a missed notice is not a failure
        log.warning("capabilities: review notice failed", exc_info=True)


__all__ = ["SubmitError", "process", "submit"]
