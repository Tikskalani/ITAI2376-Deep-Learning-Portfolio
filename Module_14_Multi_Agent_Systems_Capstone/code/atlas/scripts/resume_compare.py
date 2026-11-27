"""Blind judge: the engine's draft against the career agent's draft, same application.

  python -m scripts.resume_compare --yes                 the latest acceptance report, $0.25 at most
  python -m scripts.resume_compare --yes --run <dir>     a given data/resume_accept/<time>/ folder

This spends real money (the judge stage, Sonnet). Nothing runs without --yes, and
--cap can lower the total but never raise it past $0.25. Only pairs where both
drafts pass precheck today are judged. Each pair is judged in both orders (the
engine's judge() runs A/B then B/A and averages), with the change notes above
"# Name" stripped so the judge sees only what a recruiter would. The judge does
not know which draft is the engine's.

Prints scores and winners only; the result goes to data/resume_compare/<time>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import db
from core.config import REPO_ROOT
from core.jobs import answers, precheck, store
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, tailor

MAX_TOTAL = Decimal("0.25")
RUNS = REPO_ROOT / "data" / "resume_accept"
OUT = REPO_ROOT / "data" / "resume_compare"


def printed(text: str) -> str:
    """The resume as a recruiter sees it: everything from '# Name' on."""
    at = (text or "").find("# ")
    return text[at:] if at >= 0 else (text or "")


async def pairs(run: Path, master: str) -> list[dict[str, Any]]:
    """(application, agent draft, engine draft) where both pass precheck now."""
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    typed = await answers.resume_answers()
    out = []
    for r in report["rows"]:
        if not r.get("draft"):
            continue
        app = await store.get_application(r["application_id"])
        agent = r.get("existing_draft")
        if agent is None and app:
            resume = (app.get("materials") or {}).get("resume") or {}
            agent = None if resume.get("engine") else resume.get("text")
        if not agent:
            continue
        if not (precheck.resume_check(agent, master, typed).ok and precheck.resume_check(r["draft"], master, typed).ok):
            continue
        out.append({"application_id": r["application_id"], "company": r["company"], "role": r["role"],
                    "posting_id": (app or {}).get("posting_id"), "agent": printed(agent), "engine": printed(r["draft"])})
    return out


async def compare(
    items: list[dict[str, Any]],
    *,
    total_cap: Decimal,
    complete: bridge.CompleteFn | None = None,
    config: bridge.EngineConfig | None = None,
) -> dict[str, Any]:
    if total_cap > MAX_TOTAL:
        raise ValueError(f"the comparison is capped at ${MAX_TOTAL}")
    spent = Decimal("0")
    rows = []
    for it in items:
        left = total_cap - spent
        if left < Decimal("0.02"):
            rows.append(dict(it, skipped=f"the ${total_cap} total is spent"))
            continue
        posting = await store.get_posting(it["posting_id"]) if it.get("posting_id") else None
        target = tailor.base_target(posting or {"title": it["role"], "description": ""}, [])
        cap, _ = await bridge.run_cap(left, daily_cap_usd=await bridge.resume_spend_today() + left)
        def judge_pair(engine_llm: bridge.EngineLLM, it: dict[str, Any] = it,
                       target: dict[str, Any] = target) -> dict[str, Any]:
            result: dict[str, Any] = E.judge(engine_llm, it["agent"], it["engine"], target)
            return result

        verdict, meter = await bridge.call_engine(judge_pair, cap_usd=cap, config=config, complete=complete)
        spent += meter.spent_usd
        row = {k: it[k] for k in ("application_id", "company", "role")}
        if verdict is None:
            row["skipped"] = meter.stopped
        else:
            row.update(agent_score=verdict.get("before"), engine_score=verdict.get("after"),
                       winner={"after": "engine", "before": "agent"}.get(str(verdict.get("better")), "tie"),
                       why=verdict.get("verdict"), spent_usd=str(meter.spent_usd))
        rows.append(row)
    judged = [r for r in rows if "winner" in r]
    return {"spent_usd": str(spent), "total_cap_usd": str(total_cap), "pairs": len(items), "judged": len(judged),
            "engine_wins": sum(r["winner"] == "engine" for r in judged),
            "agent_wins": sum(r["winner"] == "agent" for r in judged),
            "ties": sum(r["winner"] == "tie" for r in judged), "rows": rows}


async def main(args: argparse.Namespace) -> int:
    cap = Decimal(args.cap)
    if cap > MAX_TOTAL:
        print(f"Refused: the comparison is capped at ${MAX_TOTAL}.", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"This spends real money (up to ${cap}). Run again with --yes.", file=sys.stderr)
        return 2
    try:
        run = Path(args.run) if args.run else max((p for p in RUNS.glob("*") if (p / "report.json").is_file()),
                                                  default=None)
        if run is None:
            print("No acceptance report to compare. Run scripts.resume_accept first.", file=sys.stderr)
            return 1
        master = tailor.load_source(Path(args.dir) if args.dir else None).full
        result = await compare(await pairs(run, master), total_cap=cap)
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
        path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"Spent ${result['spent_usd']} of ${result['total_cap_usd']}. Judged {result['judged']} of "
              f"{result['pairs']} pairs: engine {result['engine_wins']}, agent {result['agent_wins']}, "
              f"ties {result['ties']}.")
        for r in result["rows"]:
            print(f"- {r['company']}: " + (f"agent {r['agent_score']}, engine {r['engine_score']} ({r['winner']})"
                                           if "winner" in r else f"skipped: {r['skipped']}"))
        print(f"Written to {path}")
        return 0
    except tailor.TailorError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="spend real money")
    p.add_argument("--cap", default=str(MAX_TOTAL), help="total USD (at most 0.25)")
    p.add_argument("--run", help="an acceptance run folder (default: the latest)")
    p.add_argument("--dir", help="folder holding the master resume (default data/resume)")
    sys.exit(asyncio.run(main(p.parse_args())))
