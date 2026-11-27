"""The resume engine's offline bench: APEX's nine synthetic cases, scripted replies, no API.

Each case runs through the real path, bridge.call_engine (the stage routing, the
locked cap, the one-page budget) and apex_engine.optimize, with a scripted model
that tries everything a careless or hostile writer would. Per bullet it proposes,
in turn: a faithful edit, an invented number, an unsupported keyword, a faithful
edit. The summary is rewritten with one of the case's confirmed facts, and a
skills line gets an item the candidate never listed. Rejected edits come back
unchanged on the retry. What is scored:

  no_invented_rate       cases whose output claims nothing the resume and facts
                         lack (apex_benchmark.invented, the engine's own gate
                         run over the whole output)
  attack_blocked_rate    invented numbers, unsupported keywords and added skills
                         that never reach the output
  faithful_kept_rate     faithful edits that do reach it (a gate that refused
                         everything would score 1.0 above and fail here)
  evidence_used_rate     summaries rewritten from a confirmed fact that ship
                         (evidence passed separately from the text is used)
  summary_kept_rate      summaries the writer collapsed to a short fragment
                         where the original ships instead (Atlas's summary
                         guard, core/resume_engine/guards.py)
  protected_intact_rate  cases where every line outside the editable blocks
                         comes out unchanged
  questions_ok_rate      cases with at most eight questions, each on a real line
  under_cap_rate         cases whose spend stayed within the run's cap

The cases are synthetic people (bench/cases in APEX, 555 numbers, @email.com).
Scores here measure the gate and the plumbing, not writing quality: that needs
real models and judges, which is what scripts/resume_accept.py is for.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import llm
from core.resume_engine import apex_engine as E
from core.resume_engine import bridge, guards

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
CAP = Decimal("0.40")
INVENTED_NUMBER = "for 37 partner teams"
ADDED_SKILL = "Haskell"

_spec = importlib.util.spec_from_file_location("apex_benchmark", HERE / "apex_benchmark.py")
assert _spec and _spec.loader
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


def load_cases() -> list[dict[str, Any]]:
    out = []
    for f in sorted(CASES.glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        c["resume_text"] = (CASES / c["resume"]).read_text(encoding="utf-8")
        out.append(c)
    return out


def make_target(case: dict[str, Any]) -> dict[str, Any]:
    """A fixed target per case: a keyword is supported when the resume has it as a whole word."""
    text = case["resume_text"].lower()
    sup = [k for k in case["keywords"] if re.search(rf"(?<![\w-]){re.escape(k.lower())}(?![\w-])", text)]
    return {"title": case["title"], "seniority": case.get("seniority", ""), "sector": case.get("sector", "general"),
            "domain": E.apex_domains.detect(case["title"]), "job_desc": case.get("job_desc", ""),
            "keywords": [{"name": k, "pct": None} for k in case["keywords"]], "supported": sup,
            "unsupported": [k for k in case["keywords"] if k not in sup], "facts": case.get("facts") or []}


def _toggle(text: str) -> str:
    return text[:-1] if text.endswith(".") else text + "."


class Script:
    """The adversarial writer for one case. plan maps block id -> (kind, lines)."""

    def __init__(self, case: dict[str, Any], target: dict[str, Any], *, collapse: bool = False) -> None:
        doc = E.Doc(case["resume_text"])
        doc.attach_facts(target["facts"])
        self.plan: dict[str, tuple[str, list[str]]] = {}
        bullets = [b for b in doc.blocks if b["kind"] == "bullet"]
        # An attack keyword the resume really lacks: the whole-word matcher above calls
        # "design systems" unsupported when the resume says "design system", which the
        # engine rightly accepts. So pick one the engine's gate refuses on its own.
        unsupported = next((k for k in target["unsupported"]
                            if E.fact_gate(f"Built it using {k}.", case["resume_text"], target["unsupported"],
                                           case["resume_text"])), None)
        for n, b in enumerate(bullets, start=1):
            kind = ("faithful", "invented", "keyword", "faithful")[(n - 1) % 4]
            if kind == "keyword" and not unsupported:
                kind = "invented"
            line = {"faithful": _toggle(b["text"]),
                    "invented": b["text"].rstrip(".") + f" {INVENTED_NUMBER}.",
                    "keyword": b["text"].rstrip(".") + f", using {unsupported}."}[kind]
            self.plan[b["id"]] = (kind, [line])
        fact = next((f for f in target["facts"] if re.search(r"\d", f)), None)
        summary = doc.blocks[0]["text"] if doc.blocks and doc.blocks[0]["kind"] == "summary" else ""
        if summary and collapse and len(summary.split()) >= 12:
            # The failure seen on real runs: a full summary rewritten as one short line.
            self.plan["S"] = ("collapse", [" ".join(summary.split()[-6:])])
        elif fact and summary:
            self.plan["S"] = ("evidence", [summary.rstrip(".") + ". " + fact])
        s_block = doc.by_id.get("S") or {}
        # The summary as it sits in the source (it may wrap over several lines).
        self.summary_lines = [ln for ln in doc.lines[s_block.get("start", 0):s_block.get("end", 0)] if ln.strip()]
        if doc.skills and ADDED_SKILL.lower() not in case["resume_text"].lower():
            self.plan[doc.skills[0]["id"]] = ("skill", [doc.skills[0]["text"].rstrip(".") + f", {ADDED_SKILL}"])
        self.question_ids = [b["id"] for b in bullets[:3]]

    def reply(self, stage: str, user: str) -> str:
        if stage == "diagnose":
            return json.dumps({"blocks": [
                {"id": i, "impact": 2, "specificity": 2, "scannability": 3, "relevance": 4, "issues": ["vague"],
                 "question": f"What changed because of line {i}?"} for i in self.question_ids],
                "document": {"problems": [], "strengths": [], "summary_verdict": "", "most_relevant": []}})
        if stage == "rewrite":
            retry = "PREVIOUS ATTEMPT REJECTED" in user
            ids = [i for i in self.plan if f"[{i}]" in user or f'"{i}"' in user] if retry else list(self.plan)
            return json.dumps({"edits": [{"id": i, "lines": self.plan[i][1], "why": "stronger", "keywords": []}
                                         for i in ids], "order": []})
        if stage == "audit":
            items = json.loads(user.split("ITEMS:")[1].split("Return ONLY JSON")[0])
            return json.dumps({"results": [{"id": it["id"], "ok": True, "problems": []} for it in items]})
        if stage == "judge":
            return json.dumps({"A": {"score": 60}, "B": {"score": 60}, "better": "tie", "why": "same"})
        if stage == "cut":
            return json.dumps({"cuts": []})
        return "{}"


def _complete(script: Script):
    async def complete(*, system, user, tier, provider, max_tokens, purpose, effort, on_usage, **_):
        model = llm.resolve_model(tier, provider=provider)
        worst = bridge.worst_case_usd(model, system, user, max_tokens)
        on_usage(model, {"input_tokens": 1}, (worst * Decimal("0.05")).quantize(Decimal("0.0001")))
        return script.reply(purpose.removeprefix(bridge.PURPOSE_PREFIX), user)
    return complete


def _protected_lines(text: str) -> list[str]:
    doc = E.Doc(text)
    inside = {i for b in doc.blocks + doc.skills for i in range(b["start"], b["end"])}
    return [ln for i, ln in enumerate(doc.lines) if i not in inside and ln.strip()]


async def _run_case(case: dict[str, Any], collapse: bool = False) -> dict[str, Any]:
    target = make_target(case)
    script = Script(case, target, collapse=collapse)
    config = bridge.EngineConfig(stages={}, default=bridge.Route(tier="scan"), page_words=580)

    def go(engine_llm: bridge.EngineLLM) -> dict[str, Any]:
        out: dict[str, Any] = E.optimize(engine_llm, case["resume_text"], target, judge_it=True)
        guards.summary_guard(case["resume_text"], out)  # what runner.optimize applies
        return out

    result, meter = await bridge.call_engine(go, cap_usd=CAP, config=config, complete=_complete(script))
    return {"case": case, "target": target, "script": script, "result": result, "meter": meter}


def run() -> Any:
    from evals.offline import Report

    # Every other case gets the collapsed summary; the rest the summary from a fact.
    runs = [asyncio.run(_run_case(c, collapse=bool(i % 2))) for i, c in enumerate(load_cases())]
    misses: list[str] = []
    n = len(runs)
    clean = blocked = attacks = kept = faithful = used = evidence = intact = q_ok = capped = 0
    collapsed = guarded = 0
    for r in runs:
        case, target, script, result, meter = r["case"], r["target"], r["script"], r["result"], r["meter"]
        name = case["name"]
        if result is None:
            misses.append(f"{name}: the run stopped: {meter.stopped}")
            continue
        final = result["final"]
        evidence_case = dict(case, resume_text=case["resume_text"] + "\n" + "\n".join(target["facts"]))
        invented = B.invented(E, final, evidence_case, target)
        if invented:
            misses.append(f"{name}: invented {invented[:3]}")
        else:
            clean += 1
        for i, (kind, lines) in script.plan.items():
            if kind in ("invented", "keyword", "skill"):
                attacks += 1
                bad = {"invented": INVENTED_NUMBER, "skill": ADDED_SKILL}.get(kind) or lines[0].rsplit("using ", 1)[-1]
                if bad.rstrip(".") in final:
                    misses.append(f"{name}: {kind} edit on {i} reached the output")
                else:
                    blocked += 1
            elif kind == "faithful":
                faithful += 1
                if lines[0] in final:
                    kept += 1
                else:
                    misses.append(f"{name}: faithful edit on {i} was lost")
            elif kind == "collapse":
                collapsed += 1
                out_lines = final.split("\n")
                if all(ln in out_lines for ln in script.summary_lines) and lines[0] not in out_lines:
                    guarded += 1
                else:
                    misses.append(f"{name}: a collapsed summary shipped")
            elif kind == "evidence":
                evidence += 1
                if lines[0] in final:
                    used += 1
                else:
                    misses.append(f"{name}: summary from a confirmed fact was refused")
        before, after = _protected_lines(case["resume_text"]), _protected_lines(final)
        if all(ln in final.split("\n") for ln in before):
            intact += 1
        else:
            misses.append(f"{name}: a protected line changed ({len(before)} before, {len(after)} after)")
        qs = result.get("questions") or []
        if len(qs) <= 8 and all(q.get("id") in E.Doc(case["resume_text"]).by_id for q in qs):
            q_ok += 1
        else:
            misses.append(f"{name}: {len(qs)} questions")
        if meter.spent_usd <= meter.cap_usd and meter.peak_usd <= meter.cap_usd:
            capped += 1
        else:
            misses.append(f"{name}: spent {meter.spent_usd} of {meter.cap_usd}")

    def ratio(a: int, b: int) -> float:
        return round(a / b, 3) if b else 1.0

    return Report("resume_engine", n, {
        "no_invented_rate": ratio(clean, n),
        "attack_blocked_rate": ratio(blocked, attacks),
        "faithful_kept_rate": ratio(kept, faithful),
        "evidence_used_rate": ratio(used, evidence),
        "summary_kept_rate": ratio(guarded, collapsed),
        "protected_intact_rate": ratio(intact, n),
        "questions_ok_rate": ratio(q_ok, n),
        "under_cap_rate": ratio(capped, n),
    }, misses)


__all__ = ["load_cases", "make_target", "run"]
