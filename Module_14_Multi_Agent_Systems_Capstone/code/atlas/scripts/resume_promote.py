"""Promote a fact from your answers into the master resume.

  python -m scripts.resume_promote          what your drafts claim only because you answered a question,
                                            with the line to paste into the master under its role
  python -m scripts.resume_promote --check  after you edit the .docx: mark every answer whose numbers
                                            the master now has as promoted

A claim from your answer is allowed in a draft and labelled "from your answer"
before you apply. Promoting it means writing it into the master yourself (the
.docx in data/resume/ is the source of truth, and Atlas never edits it). Once
it is there, precheck finds it in the master and the label goes away. Editing
the master also retires the one-page cuts: run resume_one_page propose again.
--dir reads the master from another folder (a worktree).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import db
from core.jobs import config as jobs_config
from core.jobs import precheck, resume_pdf
from core.resume_engine import one_page
from core.resume_engine import resume_format as RF

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text or "")}


async def _answered() -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select key, question, answer, context from candidate_answers "
        "where answer is not null and context->>'kind' = 'resume' order by answered_at"
    )
    return [dict(r) for r in rows]


async def _drafts() -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select id, company, role, materials->'resume'->>'text' as text from job_applications "
        "where materials->'resume'->>'text' is not null"
    )
    return [dict(r) for r in rows]


def _lines_with(text: str, claims: list[str]) -> list[tuple[str, str]]:
    """(role, line) for each draft bullet that carries one of the claims."""
    try:
        parsed = resume_pdf.parse(text)
    except resume_pdf.ResumeFormatError:
        return []
    bare = [c.removesuffix(" (from your answer)") for c in claims]
    out = []
    for s in parsed.sections:
        for line in s.paragraphs + s.bullets:
            if any(b in line for b in bare):
                out.append((s.title, line))
        for e in s.entries:
            for line in e.bullets:
                if any(b in line for b in bare):
                    out.append((e.left, line))
    return out


async def listing(master: str) -> int:
    rows = await _answered()
    pending = [r for r in rows if not (r["context"] or {}).get("promoted_at")]
    if not pending:
        print("No answers waiting to be promoted.")
        return 0
    drafts = await _drafts()
    for r in pending:
        print(f"\n{r['question']}\n  your answer: {r['answer']}")
        shown = False
        for d in drafts:
            claims = precheck.answer_backed(d["text"], master, [f"{r['question']} {r['answer']}"])
            for role, line in _lines_with(d["text"], claims):
                shown = True
                print(f"  used in the {d['company']} draft. Paste under {role}:\n    {line}")
        if not shown:
            print("  no saved draft rests on it yet")
    print("\nAfter editing the .docx: python -m scripts.resume_promote --check")
    return 0


async def check(master: str, directory: Path) -> int:
    have = _numbers(master)
    promoted = 0
    for r in await _answered():
        ctx = r["context"] or {}
        nums = _numbers(r["answer"])
        if ctx.get("promoted_at") or not nums or not nums <= have:
            continue
        await db.execute(
            "update candidate_answers set context = context || $2::jsonb, updated_at = now() where key = $1",
            r["key"], {"promoted_at": _now()},
        )
        promoted += 1
        print(f"promoted: {r['answer']}")
    print(f"{promoted} answer(s) now in the master.")
    try:
        one_page.load(master, directory)
    except one_page.OnePageError as exc:
        print(f"The one-page cuts need approving again: {exc}")
    return 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def main(args: argparse.Namespace) -> int:
    directory = Path(args.dir) if args.dir else jobs_config.RESUME_DIR
    try:
        master = RF.master_format(directory)
        return await (check(master, directory) if args.check else listing(master))
    except RF.FormatMismatch as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="mark answers the master now contains as promoted")
    p.add_argument("--dir", help="folder holding the master resume (default data/resume)")
    sys.exit(asyncio.run(main(p.parse_args())))
