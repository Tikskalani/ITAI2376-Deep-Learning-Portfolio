"""The job submitter (auto-apply PR D): approved queue items go in.

Nothing reaches a board without Trilok's Apply tap; this only acts on
queue items he approved. For each one:

  1. Claim the posting in job_submissions. The posting is the key, so a
     retry, a second tap or a second worker never submits twice. A row
     left in 'submitting' by a crash is never retried: it may have gone
     through, and a second application is worse than a missing one.
  2. The pre-submit check (core/jobs/precheck.py). A failure sends the item
     back to "needs a look" with the reason.
  3. Evidence: the exact PDF and cover note are written to
     data/applications/<submission>/attempt-<n>/ with their SHA-256, and
     every answer typed into the form is kept. Nothing is overwritten.
  4. Fill the form in Atlas's own browser profile and screenshot it. A
     required question with no saved answer is asked once (core.jobs.answers)
     and the item waits. A CAPTCHA, login wall or emailed code is never
     bypassed: the item becomes "needs your click".
  5. Dry run (the default until Trilok has checked a week of screenshots):
     stop here. Otherwise press submit, and only a confirmation page counts
     as submitted.

A board adapter that keeps failing is switched off automatically: if fewer
than half of its last five real attempts saw a confirmation, its items go
to "needs your click" until Trilok looks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from core import db, events
from core.config import REPO_ROOT
from core.jobs import answers, precheck, resume_pdf, store
from core.jobs import config as jobs_config
from core.jobs.submit import ADAPTERS
from core.jobs.submit.base import FormPage, confirmed, obstacle

log = logging.getLogger(__name__)

EVIDENCE_DIR = REPO_ROOT / "data" / "applications"
DEFAULTS: dict[str, Any] = {"dry_run": True, "headless": True, "per_run": 5,
                            "adapter_window": 5, "adapter_min_success": 0.5}
# Statuses a new attempt may take over. 'submitting', 'submitted' and
# 'confirmed' are final: a submit is never repeated.
RECLAIMABLE = ("checking", "needs_answer", "needs_click", "blocked", "dry_run")
NO_FORM = ("Atlas found no application form at this link; the company may take applications only "
           "on its own careers site. Open the posting and apply there")
NO_RESUME_SLOT = ("Atlas could not tell which upload on this form takes the resume, so it stopped "
                  "rather than apply without one. Open the posting and attach it there")

PageFactory = Callable[[], AbstractAsyncContextManager[FormPage]]


def settings(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """config/jobs.yaml `submit:` over DEFAULTS. Dry run unless it says false."""
    raw = (jobs_config.load() if data is None else data).get("submit") or {}
    out = dict(DEFAULTS)
    out["dry_run"] = raw.get("dry_run", True) is not False
    out["headless"] = raw.get("headless", True) is not False
    for key in ("per_run", "adapter_window"):
        try:
            out[key] = max(1, min(int(raw.get(key, DEFAULTS[key])), 25))
        except (TypeError, ValueError):
            log.warning("jobs.yaml submit.%s is not a number; using %s", key, DEFAULTS[key])
    try:
        out["adapter_min_success"] = max(0.0, min(float(raw.get("adapter_min_success", 0.5)), 1.0))
    except (TypeError, ValueError):
        pass
    return out


def _default_pages(headless: bool) -> PageFactory:
    from core.jobs.submit.browser import open_page

    return lambda: open_page(headless=headless)


# ------------------------------------------------------------------ state


async def _claim(item: dict[str, Any], ats: str, dry_run: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(claimed row, None) to go ahead, or (None, existing row) when the
    posting is already submitted or its outcome is unknown."""
    row = await db.fetchrow(
        """
        insert into job_submissions (posting_id, queue_id, application_id, ats, dry_run, status, attempts)
        values ($1, $2, $3, $4, $5, 'checking', 1)
        on conflict (posting_id) do update
          set status = 'checking', attempts = job_submissions.attempts + 1, dry_run = excluded.dry_run,
              queue_id = excluded.queue_id, application_id = excluded.application_id,
              reason = null, updated_at = now()
          where job_submissions.status = any($6::text[])
        returning *
        """,
        item["posting_id"], item["queue_id"], item["application_id"], ats, dry_run, list(RECLAIMABLE),
    )
    if row is not None:
        return dict(row), None
    existing = await db.fetchrow("select * from job_submissions where posting_id = $1", item["posting_id"])
    return None, dict(existing) if existing else None


async def _set(sub_id: UUID, status: str, reason: str | None = None, *, adapter_ok: bool | None = None,
               evidence: dict[str, Any] | None = None, only_from: str | None = None) -> bool:
    """Move a submission. `only_from` makes it a compare-and-set: it only
    moves if the row is still in that status (one worker wins a submit)."""
    res = await db.execute(
        """
        update job_submissions
           set status = $2, reason = $3, adapter_ok = coalesce($4, adapter_ok),
               evidence = case when $5::jsonb is null then evidence else evidence || jsonb_build_array($5::jsonb) end,
               submitted_at = case when $2 = 'submitted' then now() else submitted_at end,
               updated_at = now()
         where id = $1 and ($6::text is null or status = $6)
        """,
        sub_id, status, reason, adapter_ok, evidence, only_from,
    )
    return res.endswith("1")


async def adapter_healthy(ats: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """(ok, why) from the adapter's last real attempts."""
    rows = await db.fetch(
        "select adapter_ok from job_submissions where ats = $1 and adapter_ok is not null "
        "order by updated_at desc limit $2",
        ats, cfg["adapter_window"],
    )
    if len(rows) < cfg["adapter_window"]:
        return True, ""
    good = sum(1 for r in rows if r["adapter_ok"])
    if good / len(rows) < cfg["adapter_min_success"]:
        return False, f"the {ats} adapter saw a confirmation on only {good} of its last {len(rows)} submits"
    return True, ""


# ------------------------------------------------------------------ evidence


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_materials(folder: Path, posting: dict[str, Any], materials: dict[str, Any]) -> dict[str, Any]:
    """The exact files that go to the board, with their hashes."""
    from core.jobs.submit.greenhouse import Files

    folder.mkdir(parents=True, exist_ok=True)
    resume = resume_pdf.parse((materials.get("resume") or {}).get("text") or "")
    pdf = resume_pdf.render(resume).pdf
    resume_path = folder / resume_pdf.filename(resume, posting["company"])
    resume_path.write_bytes(pdf)
    cover_text = ((materials.get("cover") or {}).get("text") or "").strip() + "\n"
    cover_path = folder / resume_pdf.filename(resume, posting["company"]).replace(" - Resume - ", " - Cover Letter - ").replace(".pdf", ".txt")
    cover_bytes = cover_text.encode("utf-8")
    cover_path.write_bytes(cover_bytes)
    return {
        "files": Files(resume=resume_path, cover=cover_path, cover_text=cover_text.strip()),
        "record": {
            "resume": {"path": str(resume_path), "sha256": _sha(pdf)},
            "cover": {"path": str(cover_path), "sha256": _sha(cover_bytes)},
        },
    }


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(EVIDENCE_DIR)).replace("\\", "/")
    except ValueError:
        return str(path)


# ------------------------------------------------------------------ one item


async def _load(queue_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(
        """
        select q.id as queue_id, q.status as queue_status, q.posting_id, q.application_id,
               p.source, p.board, p.external_id, p.company, p.title, p.url, a.materials
          from job_queue q
          join job_postings p on p.id = q.posting_id
          left join job_applications a on a.id = q.application_id
         where q.id = $1
        """,
        queue_id,
    )
    return dict(row) if row else None


async def _mark_submitted(item: dict[str, Any], evidence_dir: Path) -> None:
    await db.execute("update job_queue set status = 'submitted', updated_at = now() where id = $1", item["queue_id"])
    await store.set_posting_status(item["posting_id"], "applied")
    if item["application_id"]:
        await db.execute(
            "update job_applications set status = 'applied', applied_at = coalesce(applied_at, now()), "
            "next_action = null, next_action_at = null, updated_at = now() where id = $1",
            item["application_id"],
        )
        await store.record_event(item["application_id"], "submitted",
                                 f"Submitted by Atlas; evidence in {_relative(evidence_dir)}")


async def submit_one(
    queue_id: UUID | str,
    *,
    pages: PageFactory | None = None,
    cfg: dict[str, Any] | None = None,
    master: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Run one approved queue item through the submitter.
    Returns {status, reason, submission_id}."""
    cfg = cfg or settings()
    item = await _load(UUID(str(queue_id)))
    if item is None or item["queue_status"] != "approved":
        return {"status": "skipped", "reason": "not an approved queue item", "submission_id": None}
    adapter = ADAPTERS.get(item["source"])
    ats = item["source"] if adapter is None else adapter.ATS
    sub, existing = await _claim(item, ats, cfg["dry_run"])
    if sub is None:
        status = existing["status"] if existing else "unknown"
        why = "already submitted" if status in ("submitted", "confirmed") else \
            "a submit was started and its outcome is unknown; check the board by hand"
        return {"status": status, "reason": why, "submission_id": existing["id"] if existing else None}
    sid: UUID = sub["id"]

    async def stop(status: str, reason: str, **kw: Any) -> dict[str, Any]:
        await _set(sid, status, reason, **kw)
        log.info("submitter: %s at %s -> %s (%s)", item["title"], item["company"], status, reason)
        await events.emit("submit.status", agent="career", title=f"{item['title']} at {item['company']}",
                          detail=reason, data={"status": status, "submission_id": str(sid)})
        return {"status": status, "reason": reason, "submission_id": sid}

    if adapter is None:
        return await stop("needs_click", manual_reason(item["source"]))
    ok, why = await adapter_healthy(ats, cfg)
    if not ok:
        return await stop("needs_click", why + "; finish this one by hand")

    materials = item["materials"] or {}
    check = await precheck.run(item, materials, master=master, client=client)
    if not check.ok:
        await db.execute(
            "update job_queue set status = 'needs_attention', error = $2, updated_at = now() where id = $1",
            item["queue_id"], "Pre-submit check: " + check.summary(),
        )
        if item["application_id"]:
            # Durable: the queue row's error clears on a redraft, this event does not.
            await store.record_event(item["application_id"], "blocked", "Pre-submit check: " + check.summary())
        return await stop("blocked", check.summary())
    if check.from_your_answer and item["application_id"]:
        await store.record_event(item["application_id"], "precheck",
                                 "Claims from your answer: " + "; ".join(check.from_your_answer))
    missing = await answers.missing_required()
    if missing:
        return await stop("needs_answer", "fill in your application answers first: " + ", ".join(missing))

    folder = EVIDENCE_DIR / str(sid) / f"attempt-{sub['attempts']}"
    written = write_materials(folder, item, materials)
    record: dict[str, Any] = {
        "attempt": sub["attempts"], "at": datetime.now(UTC).isoformat(), "dry_run": cfg["dry_run"],
        "url": adapter.application_url(item), **written["record"], "answers": {}, "screenshots": [],
    }

    def keep(outcome: str) -> dict[str, Any]:
        record["outcome"] = outcome
        (folder / "evidence.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        return record

    async def shot(page: FormPage, name: str) -> None:
        path = folder / f"{name}.png"
        try:
            # The browser returns the form in screen-high pages (shots.tile).
            pages = await page.screenshot(path) or [path]
            for p in pages:
                record["screenshots"].append(_relative(p))
            rel = _relative(pages[0]).split("/", 1)[1] if "/" in _relative(pages[0]) else _relative(pages[0])
            await events.emit("submit.shot", agent="career", title=f"{item['title']} at {item['company']}",
                              detail=name, data={"submission_id": str(sid), "path": rel,
                                                 "url": f"/api/jobs/submissions/{sid}/files/{rel}"})
        except Exception:  # noqa: BLE001 - a missing screenshot must not lose the outcome
            log.exception("submitter: screenshot %s failed", name)

    factory = pages or _default_pages(cfg["headless"])
    pressed = False
    try:
        async with factory() as page:
            await page.open(record["url"])
            if (block := await obstacle(page)):
                await shot(page, "blocked")
                return await stop("needs_click", block, evidence=keep("needs_click"))
            filled = await adapter.fill(page, item, written["files"])
            record["answers"] = filled.answers
            await shot(page, "filled")
            if not filled.answers and not filled.missing:
                # Nothing to fill: a "page not found", or a company that takes
                # applications only on its own site (2026-10-23, Cursor). Never
                # "filled", and never a press of a button on a page with no form.
                return await stop("needs_click", NO_FORM, evidence=keep("no_form"))
            if filled.uploads and not filled.resume_placed:
                # Uploads labelled only "Attach": an application without the
                # resume is worse than one Trilok finishes himself.
                return await stop("needs_click", NO_RESUME_SLOT, evidence=keep("no_resume_slot"))
            if filled.missing:
                for label in filled.missing:
                    await answers.ask(label, {"company": item["company"], "role": item["title"]})
                return await stop("needs_answer", "the form asks: " + "; ".join(filled.missing),
                                  evidence=keep("needs_answer"))
            if (block := await obstacle(page)):
                return await stop("needs_click", block, evidence=keep("needs_click"))
            if cfg["dry_run"]:
                if item["application_id"]:
                    await store.record_event(item["application_id"], "dry_run",
                                             f"Filled, not sent (dry run); screenshot in {_relative(folder)}")
                return await stop("dry_run", "filled and screenshotted; nothing was sent", evidence=keep("dry_run"))
            if not await _set(sid, "submitting", None, only_from="checking"):
                return {"status": "skipped", "reason": "another run took this submit", "submission_id": sid}
            pressed = True
            await page.press_submit()
            if await confirmed(page):
                await shot(page, "confirmation")
                await _set(sid, "submitted", None, adapter_ok=True, evidence=keep("submitted"))
                await _mark_submitted(item, folder)
                return {"status": "submitted", "reason": None, "submission_id": sid}
            await shot(page, "after-submit")
            if (block := await obstacle(page)):
                # The board stopped the submit for a human check: not sent.
                return await stop("needs_click", block, evidence=keep("needs_click"))
            return await stop("needs_click", "submit was pressed but no confirmation appeared; check the board",
                              adapter_ok=False, evidence=keep("unconfirmed"))
    except Exception as exc:  # noqa: BLE001 - one broken form must not stop the run
        log.exception("submitter: %s at %s failed", item["title"], item["company"])
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        if pressed:
            # It may have gone through. Stay in 'submitting' so nothing retries it.
            await _set(sid, "submitting", "outcome unknown after an error: " + reason, adapter_ok=False,
                       evidence=keep("unknown"))
            return {"status": "submitting", "reason": reason, "submission_id": sid}
        return await stop("needs_click", plain_reason("the form could not be filled: " + reason, ats) or reason,
                          adapter_ok=None if cfg["dry_run"] else False, evidence=keep("error"))


# ------------------------------------------------------------------ the run


async def due(cfg: dict[str, Any]) -> list[UUID]:
    """Approved items to try now: never tried; a dry run once dry run is
    off; a waiting question after Trilok answered something new."""
    rows = await db.fetch(
        """
        select q.id from job_queue q
          left join job_submissions s on s.posting_id = q.posting_id
         where q.status = 'approved'
           and (s.id is null
                or (s.status = 'dry_run' and not $1)
                or (s.status = 'needs_answer' and exists (
                      select 1 from candidate_answers c where c.answer is not null and c.updated_at > s.updated_at)))
         order by q.decided_at nulls last, q.created_at
         limit $2
        """,
        cfg["dry_run"], cfg["per_run"],
    )
    return [r["id"] for r in rows]


_BROWSER_WARNED: set[str] = set()


async def _browser_missing(waiting: int) -> dict[str, int]:
    """The preflight failed: nothing is claimed, Trilok is told once a day."""
    from core.jobs.submit.browser import preflight
    from core.surfaces.notify import notify

    check = await preflight()
    if check["ok"]:
        return {}
    day = datetime.now(UTC).date().isoformat()
    if day not in _BROWSER_WARNED:
        _BROWSER_WARNED.add(day)
        commands = "\n".join(check["commands"])
        await notify("jobs_submit", f"Applications wait: {waiting} approved, but the browser is missing",
                     f"{check['detail']} Nothing was claimed. From the Atlas folder run:\n{commands}\n"
                     "Its tile in Settings, Connections turns green once it works.", data={"view": "connect"})
    log.warning("submitter: browser not ready (%s); %d approved item(s) wait", check["detail"], waiting)
    return {"waiting_browser": waiting}


async def run(*, pages: PageFactory | None = None, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """Try every due item. Returns a count per outcome. With the real browser
    (no pages factory given) a preflight runs first: a missing browser stops
    the run before anything is claimed."""
    cfg = cfg or settings()
    counts: dict[str, int] = {}
    items = await due(cfg)
    if items and pages is None:
        waiting = await _browser_missing(len(items))
        if waiting:
            return waiting
    for qid in items:
        out = await submit_one(qid, pages=pages, cfg=cfg)
        counts[out["status"]] = counts.get(out["status"], 0) + 1
    if counts:
        await _announce(counts, cfg["dry_run"])
    return counts


async def _announce(counts: dict[str, int], dry_run: bool) -> None:
    from core.surfaces.notify import notify

    parts = []
    if counts.get("submitted"):
        parts.append(f"{counts['submitted']} submitted")
    if counts.get("dry_run"):
        parts.append(f"{counts['dry_run']} filled in dry run, nothing sent")
    if counts.get("needs_answer"):
        parts.append(f"{counts['needs_answer']} waiting on an answer")
    if counts.get("needs_click"):
        parts.append(f"{counts['needs_click']} need your click")
    if counts.get("blocked"):
        parts.append(f"{counts['blocked']} stopped by the pre-submit check")
    if not parts:
        return
    title = "Applications: " + ", ".join(parts)
    body = "Screenshots and the exact files sent are in Jobs." + (
        " Dry run is on: check the screenshots, then set submit.dry_run: false in config/jobs.yaml."
        if dry_run else ""
    )
    await notify("jobs_submit", title, body, data={"view": "jobs"})


async def scheduled_run() -> None:
    """Scheduler entry. Never raises."""
    try:
        await run()
    except Exception:  # noqa: BLE001
        log.exception("job submitter run failed")


async def status_for(posting_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """The latest submission per posting, for the queue card."""
    if not posting_ids:
        return {}
    rows = await db.fetch(
        "select posting_id, id, ats, status, reason, dry_run, attempts, evidence, submitted_at "
        "from job_submissions where posting_id = any($1::uuid[])",
        posting_ids,
    )
    out: dict[UUID, dict[str, Any]] = {}
    for r in rows:
        ev = list(r["evidence"] or [])
        last = ev[-1] if ev else {}
        out[r["posting_id"]] = {
            "id": r["id"], "status": r["status"], "reason": plain_reason(r["reason"], r["ats"]),
            # A board with no adapter can never be filled by a retry: the card offers the posting instead.
            "can_retry": r["ats"] in ADAPTERS, "dry_run": r["dry_run"],
            "attempts": r["attempts"], "submitted_at": r["submitted_at"],
            # Relative to the submission's own folder, as evidence_file() serves them.
            "screenshots": [s.split("/", 1)[1] for s in last.get("screenshots", []) if "/" in s],
            "answers": last.get("answers", {}),
        }
    return out


BROWSER_MISSING = ("The submitter's browser is not installed for the Atlas service. "
                   "Install it from Connections, then try again.")


def manual_reason(ats: str | None) -> str:
    which = "this posting's form" if ats in (None, "", "manual") else f"{str(ats).title()} forms"
    return f"Atlas does not fill {which}. The resume and cover note are ready: open the posting and paste them in."


def plain_reason(reason: str | None, ats: str | None = None) -> str | None:
    """A submitter reason in plain words. Known failures (the browser not
    installed, a board with no adapter) read as their cause and fix, whether
    the row was written before this mapping or after; anything else keeps
    its text, without a Python exception class in front."""
    if not reason:
        return reason
    low = reason.lower()
    if "executable doesn't exist" in low or "playwright install" in low:
        return BROWSER_MISSING
    if low.startswith("atlas cannot fill ") and low.endswith("the materials are ready"):
        return manual_reason(ats)
    return re.sub(r"^(the form could not be filled: )?[A-Z][A-Za-z]*(Error|Exception): ", r"\1", reason)


def evidence_file(submission_id: str, rel: str) -> Path | None:
    """A file under one submission's evidence folder, or None (no escaping it)."""
    base = (EVIDENCE_DIR / str(UUID(submission_id))).resolve()
    path = (base / rel).resolve()
    if base not in path.parents or not path.is_file():
        return None
    return path


__all__ = ["DEFAULTS", "EVIDENCE_DIR", "adapter_healthy", "due", "evidence_file", "run", "scheduled_run",
           "settings", "status_for", "submit_one", "write_materials"]
