"""The resume engine's acceptance run, on the applications in the queue.

  python -m scripts.resume_accept --yes                 draft every queued application, $1.50 at most in all
  python -m scripts.resume_accept --yes --save          same, and save the drafts and their questions
  python -m scripts.resume_accept --yes --application <id> [--application <id>]
  python -m scripts.resume_accept --yes --revise <id>   after answering its questions in Jobs: one revision

--dir reads the master (and the one-page cuts) from another folder, e.g. the
main checkout's data/resume when this runs from a worktree.

This spends real money (Haiku and Sonnet through Anthropic). Nothing runs without
--yes, and --cap can lower the total but never raise it past $1.50. Each run is
also held to the run cap ($0.40); the resume engine's daily cap is lifted to the
total for this process only, and Atlas's own daily cap still applies.

Success means: every draft the engine produces passes precheck with zero invented
claims (no number or organization the master or your typed answers lack), and the
questions it asks are ones only you can answer. The report sets each engine draft
beside the career agent's existing draft for the same application (precheck, words,
questions, cost), shows tokens per stage with cache reads and writes, and projects
what a nightly queue would cost. The console gets the summary only; the full
report, the drafts and every model reply go to data/resume_accept/<time>/
(gitignored: they hold your resume). replies.jsonl replays offline at no cost
(core/resume_engine/replay.py).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import costs, db, llm
from core.config import REPO_ROOT, settings
from core.jobs import answers, precheck, store
from core.jobs import queue as jobs_queue
from core.resume_engine import bridge, one_page, replay, tailor

MAX_TOTAL = Decimal("1.50")
OUT = REPO_ROOT / "data" / "resume_accept"
_NUMBER_QUESTION = re.compile(r"\b(how many|how much|how long|how often|what (?:share|percent|%)|by how)\b", re.I)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _words(text: str) -> int:
    return len(text[text.find("# "):].split()) if text and "# " in text else len((text or "").split())


async def queued_applications() -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select a.id, a.company, a.role, q.status
          from job_queue q join job_applications a on a.id = q.application_id
         where q.status in ('drafting', 'ready', 'needs_attention', 'approved')
         order by q.created_at
        """
    )
    return [dict(r) for r in rows]


async def agent_draft_cost(application_id: str) -> Decimal | None:
    """What the career agent's queue draft cost (its run drafted the resume and the cover note)."""
    row = await db.fetchrow(
        """
        select e.agent_name, e.what_changed->'tokens' as tokens
          from job_queue q join episodes e on e.id = q.draft_episode_id
         where q.application_id = $1
        """,
        application_id,
    )
    if not row or not row["tokens"]:
        return None
    return costs.cost_of_usage(costs.model_for_agent(row["agent_name"]), row["tokens"])


def _verdict(row: dict[str, Any]) -> str:
    if row.get("refused"):
        return f"REFUSED: {row['refused']}"
    return "PASS" if not row["invented"] and row["page_ok"] else "FAIL"


def _existing(text: str, master: str, typed: list[str]) -> dict[str, Any]:
    """The career agent's draft, scored the same way as the engine's."""
    if not text:
        return {"present": False}
    check = precheck.resume_check(text, master, typed)
    return {"present": True, "words": _words(text),
            "invented": [p for p in check.problems if "master resume does not" in p],
            "page_ok": not any("pages" in p for p in check.problems),
            "other_problems": [p for p in check.problems if "master resume does not" not in p and "pages" not in p]}


async def accept(
    app_ids: list[str],
    *,
    total_cap: Decimal,
    save: bool,
    out_dir: Path,
    complete: bridge.CompleteFn | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    """Draft each application under one total cap and score the drafts."""
    if total_cap > MAX_TOTAL:
        raise ValueError(f"the acceptance run is capped at ${MAX_TOTAL}")
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240  (a one-off command, not the server)
    complete = complete or replay.recorder(llm.complete, out_dir / "replies.jsonl")
    typed = await answers.resume_answers()
    source = tailor.load_source(directory)
    master = source.full
    spent = Decimal("0")
    rows: list[dict[str, Any]] = []
    for app_id in app_ids:
        app = await store.get_application(app_id)
        left = total_cap - spent
        run_cap = min(Decimal(str(settings.resume_run_usd_cap)), left)
        row: dict[str, Any] = {"application_id": app_id,
                               "company": app["company"] if app else "?", "role": app["role"] if app else "?"}
        existing_text = (((app or {}).get("materials") or {}).get("resume") or {}).get("text") or ""
        existing_engine = bool((((app or {}).get("materials") or {}).get("resume") or {}).get("engine"))
        row["existing"] = dict(_existing(existing_text, master, typed), by_engine=existing_engine,
                               cost_usd=str(await agent_draft_cost(app_id) or "unknown"))
        if app and app.get("posting_id"):
            posting = await store.get_posting(app["posting_id"])
            row["posting_sha256"] = _sha((posting or {}).get("description") or "")
        if run_cap < Decimal("0.05"):
            row["refused"] = f"the ${total_cap} total is spent"
            rows.append(row)
            continue
        today = await bridge.resume_spend_today()
        try:
            out = await tailor.optimize_application(
                app_id, cap_usd=run_cap, daily_cap_usd=today + left, directory=directory,
                complete=complete, save=save)
        except tailor.TailorError as exc:
            row["refused"] = str(exc)
            rows.append(row)
            continue
        cost = Decimal(out.get("spent_usd") or "0")
        spent += cost
        row.update(spent_usd=str(cost), cost=out.get("cost"))
        if not out.get("text"):
            row["refused"] = out.get("refused") or "no draft"
            rows.append(row)
            continue
        check = precheck.resume_check(out["text"], master, typed)
        qs = out.get("questions") or []
        row.update(
            invented=[p for p in check.problems if "master resume does not" in p],
            page_ok=not any("pages" in p for p in check.problems),
            other_problems=[p for p in check.problems if "master resume does not" not in p and "pages" not in p],
            from_your_answer=check.from_your_answer, words=_words(out["text"]),
            changed=out.get("changed"), left_as_written=out.get("left_as_written"),
            questions=qs, number_questions=sum(bool(_NUMBER_QUESTION.search(q)) for q in qs),
            gaps=out.get("gaps"), score=out.get("score"), veto=out.get("veto"),
            summary_guard=out.get("summary_guard"),
            fallbacks=out.get("fallbacks"), draft=out["text"], existing_draft=existing_text,
        )
        rows.append(row)
    drafted = [r for r in rows if not r.get("refused")]
    passed = [r for r in drafted if _verdict(r) == "PASS"]
    per_run = (sum((Decimal(r["spent_usd"]) for r in drafted), Decimal("0")) / len(drafted)) if drafted else None
    daily = int(jobs_queue.settings().get("daily_cap") or 0)
    nightly = None
    if per_run is not None:
        raw = per_run * daily
        nightly = {"per_run_usd": str(per_run.quantize(Decimal("0.0001"))), "queue_daily_cap": daily,
                   "per_night_usd": str(raw.quantize(Decimal("0.01"))),
                   "per_night_capped_usd": str(min(raw, Decimal(str(settings.resume_daily_usd_cap)))
                                               .quantize(Decimal("0.01"))),
                   "per_month_usd": str((min(raw, Decimal(str(settings.resume_daily_usd_cap))) * 30)
                                        .quantize(Decimal("0.01")))}
    summary = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_cap_usd": str(total_cap), "spent_usd": str(spent), "saved": save,
        "applications": len(rows), "drafted": len(drafted), "passed": len(passed),
        "with_questions": sum(1 for r in drafted if r.get("questions")),
        "success": bool(drafted) and len(passed) == len(drafted),
        "nightly": nightly,
        "inputs": {"master_sha256": one_page.master_hash(master), "input": source.kind,
                   "input_sha256": source.sha256, "typed_answers_sha256": _sha("\n".join(typed))},
    }
    (out_dir / "report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str),
                                         encoding="utf-8")
    (out_dir / "report.md").write_text(render(summary, rows, drafts=True), encoding="utf-8")
    return {"summary": summary, "rows": rows}


def _stage_lines(row: dict[str, Any]) -> list[str]:
    stages = ((row.get("cost") or {}).get("stages") or {})
    if not stages:
        return []
    out = ["  | stage | calls | $ | input | output | cache read | cache write |", "  |---|---:|---:|---:|---:|---:|---:|"]
    for k, v in stages.items():
        out.append(f"  | {k} | {v['calls']} | {v['cost_usd']} | {v.get('input', 0)} | {v.get('output', 0)} | "
                   f"{v.get('cache_read', 0)} | {v.get('cache_write', 0)} |")
    return out


def render(summary: dict[str, Any], rows: list[dict[str, Any]], *, drafts: bool) -> str:
    """The report. drafts=False leaves out every line of resume text (the console view)."""
    lines = [f"# Resume engine acceptance run, {summary['at']}", "",
             f"Spent ${summary['spent_usd']} of ${summary['total_cap_usd']}. Drafts saved: {summary['saved']}.",
             f"{summary['passed']} of {summary['drafted']} drafts pass precheck with zero invented claims; "
             f"{summary['with_questions']} asked you questions. "
             + ("SUCCESS." if summary["success"] else "NOT YET: see below.")]
    n = summary.get("nightly")
    if n:
        lines.append(f"A nightly queue of {n['queue_daily_cap']} would cost about ${n['per_night_usd']} "
                     f"(${n['per_run_usd']} a run), held to ${n['per_night_capped_usd']} by the resume-engine "
                     f"daily cap: about ${n['per_month_usd']} a month.")
    lines += ["", "| Application | Engine | Words | Questions | Engine $ | Existing draft | Words | Existing $ |",
              "|---|---|---:|---:|---:|---|---:|---:|"]
    for r in rows:
        ex = r.get("existing") or {}
        ex_v = ("none" if not ex.get("present") else
                ("PASS" if not ex.get("invented") and ex.get("page_ok") else "FAIL")
                + (" (engine)" if ex.get("by_engine") else ""))
        lines.append(f"| {r['company']}, {r['role']} | {_verdict(r)} | {r.get('words', '')} | "
                     f"{len(r.get('questions') or [])} | {r.get('spent_usd', '')} | {ex_v} | "
                     f"{ex.get('words', '')} | {ex.get('cost_usd', '')} |")
    lines.append("")
    lines.append("Existing $ is the career agent's whole queue draft (resume and cover note together).")
    for r in rows:
        lines += ["", f"## {r['company']}, {r['role']}", f"- Verdict: {_verdict(r)}"]
        if r.get("refused"):
            continue
        ex = r.get("existing") or {}
        lines += [f"- Spent ${r['spent_usd']}; lines changed {r['changed']}, left as written {r['left_as_written']}",
                  f"- Invented claims: {r['invented'] or 'none'}",
                  f"- From your answer: {r['from_your_answer'] or 'none'}",
                  f"- Other precheck notes: {r['other_problems'] or 'none'}",
                  f"- Existing draft: invented {ex.get('invented') or 'none'}; other notes "
                  f"{ex.get('other_problems') or 'none'}",
                  f"- Gaps (asked for, not on your resume): {', '.join(r['gaps'] or []) or 'none'}",
                  f"- Judge: before {(r.get('score') or {}).get('before')}, after {(r.get('score') or {}).get('after')}"
                  + (f"; VETOED: {r['veto']}" if r.get("veto") else ""),
                  f"- Summary guard: {r.get('summary_guard') or 'kept the rewrite'}",
                  f"- Fallbacks: {r['fallbacks'] or 'none'}",
                  f"- Questions ({len(r['questions'])}, {r['number_questions']} ask for a number):"]
        lines += [f"  - {q}" for q in r["questions"]] or ["  - none"]
        lines += ["- Tokens by stage:", *_stage_lines(r)]
        if drafts:
            lines += ["", "### Engine draft", "```", r["draft"], "```"]
            if r.get("existing_draft"):
                lines += ["", "### Existing draft", "```", r["existing_draft"], "```"]
    return "\n".join(lines) + "\n"


async def main(args: argparse.Namespace) -> int:
    cap = Decimal(args.cap)
    if cap > MAX_TOTAL:
        print(f"Refused: the acceptance run is capped at ${MAX_TOTAL}.", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"This spends real money (up to ${cap}). Run again with --yes.", file=sys.stderr)
        return 2
    out_dir = OUT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(args.dir) if args.dir else None
    try:
        if args.revise:
            today = await bridge.resume_spend_today()
            out = await tailor.revise_application(
                args.revise, cap_usd=min(Decimal(str(settings.resume_run_usd_cap)), cap),
                daily_cap_usd=today + cap, directory=directory)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "revise.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
            print(json.dumps({k: v for k, v in out.items() if k != "text"}, indent=2, default=str))
            print(f"\nThe revised draft is in {out_dir / 'revise.json'}")
            return 0
        ids = args.application or [str(a["id"]) for a in await queued_applications()]
        if not ids:
            print("No applications in the queue. Pass --application <id>.")
            return 1
        result = await accept(ids, total_cap=cap, save=args.save, out_dir=out_dir, directory=directory)
        print(render(result["summary"], result["rows"], drafts=False))
        print(f"The drafts and the full report: {out_dir / 'report.md'}")
        return 0 if result["summary"]["success"] else 1
    except tailor.TailorError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="spend real money")
    p.add_argument("--cap", default=str(MAX_TOTAL), help="total USD for the run (at most 1.50)")
    p.add_argument("--save", action="store_true", help="save the drafts and their questions on the applications")
    p.add_argument("--application", action="append", help="an application id (repeatable); default: the queue")
    p.add_argument("--revise", help="revise this application from the answers you typed in Jobs")
    p.add_argument("--dir", help="folder holding the master resume (default data/resume)")
    sys.exit(asyncio.run(main(p.parse_args())))
