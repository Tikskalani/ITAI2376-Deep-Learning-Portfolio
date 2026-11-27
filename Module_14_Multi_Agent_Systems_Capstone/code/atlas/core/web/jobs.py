"""Atlas -- the Jobs view's API.

  GET  /api/jobs                                funnel, applications, roles
  GET  /api/jobs/applications/{id}              one application, events, drafts
  GET  /api/jobs/applications/{id}/resume.pdf   the tailored resume, typeset
  POST /api/jobs/applications                   track one by hand
  POST /api/jobs/applications/{id}/status       move it (applied, interview...)
  POST /api/jobs/applications/{id}/draft        career agent drafts cover,
                                                resume, followup or prep
  POST /api/jobs/postings/{id}/track            a found role -> interested/applied
  POST /api/jobs/postings/{id}/dismiss          hide a found role
  GET  /api/jobs/answers                        the answers applications use
  POST /api/jobs/answers                        save one (or clear it)
  POST /api/jobs/answers/forget                 drop a learned question
  GET  /api/jobs/queue                          today's apply queue
  POST /api/jobs/queue/build                    fill today's queue now
  POST /api/jobs/queue/apply-all                approve every ready item
  POST /api/jobs/queue/{id}/decide              apply or skip one
  POST /api/jobs/queue/{id}/redraft             draft a stuck item again
  POST /api/jobs/queue/{id}/submit              run the submitter on one approved item now
  GET  /api/jobs/submissions/{id}/files/{path}  a screenshot or file from a submission's evidence

Drafting (draft, queue build, redraft) runs the career agent (Sonnet) and
costs money; every other route is a database read or write. Nothing here
sends anything anywhere: Apply records the decision for the submitter.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core import db
from core.jobs import answers, resume_pdf, store, submitter
from core.jobs import queue as apply_queue
from core.jobs.mail import PLACEHOLDER_COMPANY, shown_company

router = APIRouter()


def _uuid_or_404(value: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


@router.get("/api/jobs")
async def jobs_overview(min_score: int = 50) -> dict[str, Any]:
    apps = await store.list_applications(limit=80)
    keep = ("id", "company", "role", "status", "applied_at", "last_contact_at", "next_action", "next_action_at", "url")
    # A role in the apply queue is shown there, not twice.
    queued = {str(r["posting_id"]) for r in await apply_queue.queued_posting_ids()}
    roles = [r for r in one_per_role(await store.top_postings(max(0, min(min_score, 100)), 60))
             if not set(r["ids"]) & queued]
    pkeep = ("id", "company", "title", "location", "remote", "salary_min", "salary_max", "score", "reason", "url",
             "status", "first_seen_at")
    return {
        "funnel": await store.funnel(),
        "applications": [{**{k: a.get(k) for k in keep}, "company": shown_company(a.get("company")),
                          "drafts": sorted((a.get("materials") or {}).keys())}
                         for a in apps],
        "postings": [{**{k: p.get(k) for k in pkeep}, "company": shown_company(p.get("company")),
                      "locations": p["locations"]} for p in roles][:25],
    }


def one_per_role(postings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card per role: the same company and title in three cities is one
    role with three locations (audit finding 15: OpenAI's API Platform role
    showed twice, Databricks' FDE roles up to three times). Keeps the
    best-scored posting of each and the order it came in."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for p in postings:
        company = " ".join(str(p.get("company") or "").lower().split())
        if not company or PLACEHOLDER_COMPANY.match(company):
            company = "#" + str(p.get("id"))  # an unnamed employer is never merged with another
        key = (company, " ".join(str(p.get("title") or "").lower().split()))
        loc = (p.get("location") or "").strip()
        if key in seen:
            first = seen[key]
            first["ids"].append(str(p.get("id")))
            if loc and loc not in first["locations"]:
                first["locations"].append(loc)
            continue
        seen[key] = {**p, "locations": [loc] if loc else [], "ids": [str(p.get("id"))]}
    return list(seen.values())


@router.get("/api/jobs/applications/{app_id}")
async def jobs_application(app_id: str) -> dict[str, Any]:
    app = await store.get_application(_uuid_or_404(app_id))
    if not app:
        raise HTTPException(status_code=404, detail="no such application")
    return {**app, "company": shown_company(app.get("company"))}


@router.get("/api/jobs/applications/{app_id}/resume.pdf")
async def jobs_resume_pdf(app_id: str) -> Response:
    app = await store.get_application(_uuid_or_404(app_id))
    if not app:
        raise HTTPException(status_code=404, detail="no such application")
    draft = (app.get("materials") or {}).get("resume") or {}
    if not draft.get("text"):
        raise HTTPException(status_code=404, detail="no tailored resume yet; draft one first")
    try:
        parsed = resume_pdf.parse(draft["text"])
    except resume_pdf.ResumeFormatError as exc:
        raise HTTPException(status_code=422, detail=f"the saved resume is not in the PDF format: {exc}") from exc
    # Rendering is CPU-bound (a few tenths of a second); keep it off the loop.
    rendered = await asyncio.to_thread(resume_pdf.render, parsed)
    name = resume_pdf.filename(parsed, app.get("company"))
    return Response(
        rendered.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "X-Resume-Pages": str(rendered.pages),
            "Cache-Control": "no-store",
        },
    )


class NewApplication(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    status: Literal["interested", "applied"] = "applied"


@router.post("/api/jobs/applications")
async def jobs_add(req: NewApplication) -> dict[str, Any]:
    row = await store.upsert_application(req.company, req.role, status=req.status, source="manual", url=req.url)
    await store.record_event(row["id"], req.status, "Added from the dashboard")
    return {"application": row}


class StatusChange(BaseModel):
    status: Literal["interested", "applied", "screening", "interview", "offer", "rejected", "withdrawn", "ghosted"]
    note: str = Field(default="", max_length=2000)


@router.post("/api/jobs/applications/{app_id}/status")
async def jobs_status(app_id: str, req: StatusChange) -> dict[str, Any]:
    row = await store.set_status(_uuid_or_404(app_id), req.status, req.note)
    if not row:
        raise HTTPException(status_code=404, detail="no such application")
    if req.status in ("rejected", "withdrawn", "offer"):
        await store.set_next_action(row["id"], None, None)
    return {"application": row}


class DraftRequest(BaseModel):
    kind: Literal["cover", "resume", "followup", "prep", "referral"]


_DRAFT_ASK = {
    "cover": "a cover note for this application",
    "resume": "a tailored one-page resume for this application",
    "followup": "a short follow-up email for this application",
    "prep": "an interview prep pack for this application",
    "referral": "a referral ask to someone at this company",
}


@router.post("/api/jobs/applications/{app_id}/draft")
async def jobs_draft(app_id: str, req: DraftRequest) -> dict[str, Any]:
    from core.runtime.dispatcher import dispatch

    app = await store.get_application(_uuid_or_404(app_id))
    if not app:
        raise HTTPException(status_code=404, detail="no such application")
    posting = f" The posting is {app['posting_id']}; read it with get_posting." if app.get("posting_id") else ""
    task = (
        f"Draft {_DRAFT_ASK[req.kind]}: {app['role']} at {app['company']} (application {app['id']}).{posting} "
        f"Read the resume first. Save it with save_materials kind '{req.kind}', then reply with the draft "
        "and, in one line, anything Trilok must check or fill in."
    )
    result = await dispatch("career", task, dispatched_by="web/jobs", participants=["owner", "career"])
    fresh = await store.get_application(app["id"])
    return {"reply": result.text, "materials": (fresh or {}).get("materials") or {}}


class TrackRequest(BaseModel):
    status: Literal["interested", "applied"] = "interested"


@router.post("/api/jobs/postings/{posting_id}/track")
async def jobs_track(posting_id: str, req: TrackRequest) -> dict[str, Any]:
    p = await store.get_posting(_uuid_or_404(posting_id))
    if not p:
        raise HTTPException(status_code=404, detail="no such posting")
    row = await store.upsert_application(
        p["company"], p["title"], status=req.status, source="discovery", url=p.get("url"), posting_id=p["id"],
    )
    await store.record_event(row["id"], req.status, f"From a found role (score {p.get('score')})")
    await store.set_posting_status(p["id"], "applied" if req.status == "applied" else "shown")
    return {"application": row}


@router.post("/api/jobs/postings/{posting_id}/dismiss")
async def jobs_dismiss(posting_id: str) -> dict[str, Any]:
    pid = _uuid_or_404(posting_id)
    if not await store.get_posting(pid):
        raise HTTPException(status_code=404, detail="no such posting")
    await store.set_posting_status(pid, "dismissed")
    return {"dismissed": pid}


@router.get("/api/jobs/answers")
async def jobs_answers() -> dict[str, Any]:
    return await answers.overview()


class AnswerIn(BaseModel):
    key: str = Field(min_length=1, max_length=210)
    answer: str = Field(default="", max_length=answers.MAX_ANSWER)
    question: str | None = Field(default=None, max_length=answers.MAX_QUESTION)


@router.post("/api/jobs/answers")
async def jobs_answer(req: AnswerIn) -> dict[str, Any]:
    try:
        row = await answers.set_answer(req.key, req.answer, req.question)
    except answers.AnswerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"answer": row}


class ForgetIn(BaseModel):
    key: str = Field(min_length=1, max_length=210)


@router.post("/api/jobs/answers/forget")
async def jobs_answer_forget(req: ForgetIn) -> dict[str, Any]:
    try:
        gone = await answers.forget(req.key)
    except answers.AnswerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not gone:
        raise HTTPException(status_code=404, detail="no such question")
    return {"forgotten": req.key}



# ------------------------------------------------------------------ the daily queue

# One build at a time per process; a build drafts with the career agent and
# takes minutes, so the route starts it and the page polls the listing.
_build_task: asyncio.Task[Any] | None = None
# Same for the submitter: a browser run takes a minute per form.
_submit_task: asyncio.Task[Any] | None = None


def _start_submit(coro: Any) -> bool:
    global _submit_task
    if _submit_task and not _submit_task.done():
        coro.close()
        return False
    _submit_task = asyncio.create_task(coro)
    return True


@router.get("/api/jobs/queue")
async def jobs_queue() -> dict[str, Any]:
    cfg = apply_queue.settings()
    items = await apply_queue.listing()
    subs = await submitter.status_for([i["posting_id"] for i in items])
    for i in items:
        i["submission"] = subs.get(i["posting_id"])
        i["company"] = shown_company(i.get("company"))
    return {
        "items": items,
        "building": bool(_build_task and not _build_task.done()),
        "submitting": bool(_submit_task and not _submit_task.done()),
        "dry_run": submitter.settings()["dry_run"],
        "queued_today": await apply_queue.queued_today(),
        "daily_cap": cfg["daily_cap"],
        "min_score": cfg["min_score"],
        # Shown beside the threshold, so an empty queue says why ("needs 80+, best this week 70").
        "best_score": await db.fetchval(
            "select max(score) from job_postings where status in ('new', 'shown') "
            "and first_seen_at > now() - interval '7 days'"),
    }


@router.post("/api/jobs/queue/build")
async def jobs_queue_build() -> dict[str, Any]:
    global _build_task
    if _build_task and not _build_task.done():
        return {"started": False, "reason": "already building"}
    if await apply_queue.queued_today() >= apply_queue.settings()["daily_cap"]:
        return {"started": False, "reason": "today's queue is full"}
    _build_task = asyncio.create_task(apply_queue.build())
    return {"started": True}


@router.post("/api/jobs/queue/apply-all")
async def jobs_queue_apply_all() -> dict[str, Any]:
    approved = await apply_queue.apply_all()
    if approved:
        _start_submit(submitter.run())
    return {"approved": approved}


class QueueDecision(BaseModel):
    decision: Literal["apply", "skip"]


@router.post("/api/jobs/queue/{queue_id}/decide")
async def jobs_queue_decide(queue_id: str, req: QueueDecision) -> dict[str, Any]:
    row = await apply_queue.decide(_uuid_or_404(queue_id), req.decision)
    if row is None:
        raise HTTPException(status_code=409, detail="already decided, or not ready to apply yet")
    if req.decision == "apply":
        _start_submit(submitter.run())  # the tap is the approval; no need to wait for the next tick
    return {"item": row}


@router.post("/api/jobs/queue/{queue_id}/redraft")
async def jobs_queue_redraft(queue_id: str) -> dict[str, Any]:
    status = await apply_queue.redraft(_uuid_or_404(queue_id))
    if status is None:
        raise HTTPException(status_code=409, detail="only an item that needs attention can be drafted again")
    return {"status": status}


class PasteRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    company: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=300)
    apply: bool = True


# Paste jobs run to the end even if the page that started them closes; the set
# keeps a reference so the event loop does not drop them mid-draft.
_paste_tasks: set[asyncio.Task[Any]] = set()


@router.post("/api/jobs/paste")
async def jobs_paste(req: PasteRequest) -> dict[str, Any]:
    """A pasted job link (core/jobs/paste.py): found, tracked, queued, then
    drafted and, when asked and the board is one Atlas fills, applied."""
    from core.jobs import paste

    try:
        item = await paste.take(req.url, company=req.company, title=req.title)
    except paste.PasteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item["duplicate"]:
        task = asyncio.create_task(paste.draft_and_apply(item, apply=req.apply))
        _paste_tasks.add(task)
        task.add_done_callback(_paste_tasks.discard)
    return {**item, "company": shown_company(item["company"]),
            "queue_id": str(item["queue_id"]), "application_id": str(item["application_id"]),
            "posting_id": str(item.get("posting_id") or "")}


@router.post("/api/jobs/queue/{queue_id}/submit")
async def jobs_queue_submit(queue_id: str) -> dict[str, Any]:
    """Try one approved item now (after a CAPTCHA, a fixed answer or a redraft)."""
    started = _start_submit(submitter.submit_one(_uuid_or_404(queue_id)))
    return {"started": started, "reason": None if started else "already submitting"}


@router.get("/api/jobs/submissions/{submission_id}/files/{path:path}")
async def jobs_submission_file(submission_id: str, path: str) -> FileResponse:
    try:
        found = submitter.evidence_file(submission_id, path)
    except ValueError:
        found = None
    if found is None:
        raise HTTPException(status_code=404, detail="no such evidence file")
    return FileResponse(found)


__all__ = ["router"]
