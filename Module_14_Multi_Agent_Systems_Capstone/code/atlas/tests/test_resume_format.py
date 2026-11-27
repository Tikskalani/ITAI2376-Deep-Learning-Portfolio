"""The seam between resume_pdf's format and the APEX engine.

Format -> engine text -> format must be lossless, the engine must read every
bullet, role and section the way resume_pdf does, and an engine result whose
fixed lines drifted must be refused. The synthetic cases run everywhere; the
master-resume cases run on Trilok's laptop, where data/resume/ holds the real
.docx (ATLAS_MASTER_RESUME_DIR points elsewhere, e.g. from a worktree), and skip
in CI. No model is called anywhere in this file.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from core.jobs import config as jobs_config
from core.jobs import resume_pdf
from core.resume_engine import apex_engine as E
from core.resume_engine import resume_format as RF

FORMAT_TEXT = """\
# Jo Doe
Austin, TX | 555-0100 | jo@example.com
## SUMMARY
AI engineer who ships tested agent systems for small businesses.
## SKILLS
Agents: tool use, guardrails, evals
Languages: Python, TypeScript, SQL
## EXPERIENCE
### Acme Robotics, Austin, TX | 2024 to present
Senior AI Engineer
- Built a support agent on the Claude API used by 40 staff across three offices.
- Wrote 300 integration tests against a real Postgres and ran them in CI.
### Beta Logistics, Austin, TX | 2019 to 2024
- Automated invoice matching for 1,200 vendors with a Python service.
## PROJECTS
### DocChat, RAG over PDFs | 2025
- Answers questions over uploaded PDFs with citations and a held-out eval set.
## EDUCATION
### Austin Community College, Austin, TX | 2023
Associate of Applied Science, Computer Science"""


# ------------------------------------------------------------------ docx -> format


def _docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """A minimal .docx. kinds: plain, bold, list, tab (left<TAB>right, bold)."""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    for kind, text in paragraphs:
        ppr = ""
        if kind == "list":
            ppr = ('<w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/>'
                   '<w:numId w:val="2"/></w:numPr></w:pPr>')
        rpr = "<w:rPr><w:b/></w:rPr>" if kind in ("bold", "tab") else ""
        if kind == "tab":
            left, right = text.split("\t")
            runs = f"<w:r>{rpr}<w:t>{left}</w:t></w:r><w:r><w:tab/><w:t>{right}</w:t></w:r>"
        else:
            runs = f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'
        body.append(f"<w:p>{ppr}{runs}</w:p>")
    doc = f'<?xml version="1.0"?><w:document {w}><w:body>{"".join(body)}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_docx_converts_to_the_resume_format(tmp_path):
    path = tmp_path / "Jo Doe Resume.docx"
    path.write_bytes(_docx([
        ("bold", "Jo Doe"),
        ("plain", "Austin, TX | 555-0100 | jo@example.com"),
        ("bold", "SUMMARY"),
        ("plain", "AI engineer who ships tested agent systems for small businesses."),
        ("bold", "SKILLS"),
        ("bold", "Agents: tool use, guardrails, evals"),
        ("bold", "Languages: Python, TypeScript, SQL"),
        ("bold", "EXPERIENCE"),
        ("tab", "Acme Robotics, Austin, TX\t2024 to present"),
        ("plain", "Senior AI Engineer"),
        ("list", "Built a support agent on the Claude API used by 40 staff across three offices."),
        ("list", "Wrote 300 integration tests against a real Postgres and ran them in CI."),
        ("tab", "Beta Logistics, Austin, TX\t2019 to 2024"),
        ("list", "Automated invoice matching for 1,200 vendors with a Python service."),
        ("bold", "PROJECTS"),
        ("tab", "DocChat, RAG over PDFs\t2025"),
        ("list", "Answers questions over uploaded PDFs with citations and a held-out eval set."),
        ("bold", "EDUCATION"),
        ("tab", "Austin Community College, Austin, TX\t2023"),
        ("plain", "Associate of Applied Science, Computer Science"),
    ]))
    assert RF.docx_to_format(path) == FORMAT_TEXT
    assert RF.master_format(tmp_path) == FORMAT_TEXT


def test_a_docx_without_sections_is_refused(tmp_path):
    path = tmp_path / "r.docx"
    path.write_bytes(_docx([("bold", "Jo Doe"), ("plain", "Just a paragraph.")]))
    with pytest.raises(RF.FormatMismatch, match="no section headers"):
        RF.docx_to_format(path)


def test_no_master_is_a_clear_error(tmp_path):
    with pytest.raises(RF.FormatMismatch, match="no .docx"):
        RF.master_format(tmp_path)


# ------------------------------------------------------------------ the round trip


def _check_lossless(text: str) -> E.Doc:
    """Every property the seam promises, on one FORMAT text. Returns the engine's Doc."""
    resume_pdf.parse(text)
    assert RF.alignment_problems(text) == []
    engine_text = RF.to_engine(text)
    assert engine_text == text
    doc = E.Doc(engine_text)
    assert doc.render({}) == engine_text, "the engine re-renders its input byte for byte"
    assert RF.from_engine(doc.render({}), text) == text

    parsed = resume_pdf.parse(text)
    editable = [s for s in parsed.sections if E.EDITABLE_SECTIONS.search(s.title)]
    format_bullets = [b for s in editable for e in s.entries for b in e.bullets]
    engine_bullets = [b["text"] for b in doc.blocks if b["kind"] == "bullet"]
    assert engine_bullets == format_bullets, "one engine bullet per format bullet, same text, same order"
    skills = [p for s in parsed.sections if E.SKILL_SECTIONS.search(s.title)
              and not E.EDITABLE_SECTIONS.search(s.title) for p in s.paragraphs]
    assert [k["text"] for k in doc.skills] == skills
    summary = [p for s in parsed.sections if E.SUMMARY_SECTIONS.match(s.title) for p in s.paragraphs]
    if summary:
        assert doc.blocks[0]["id"] == "S" and doc.blocks[0]["text"] == " ".join(summary)
    for s in editable:
        for e in s.entries:
            heading = f"### {e.left}" + (f" | {e.right}" if e.right else "")
            mine = [b for b in doc.blocks if b["kind"] == "bullet" and b["group"].startswith(heading)]
            assert [b["text"] for b in mine] == e.bullets, f"bullets of {e.left!r} stay in their role"
    return doc


def _check_edits_survive(text: str, doc: E.Doc) -> None:
    """Engine-shaped edits (rewrite, split, skills reorder, bullet order, section order)
    come back as valid FORMAT with every fixed line unchanged."""
    bullets = [b for b in doc.blocks if b["kind"] == "bullet"]
    edits: dict[str, list[str]] = {b["id"]: [b["text"] + " Rewritten."] for b in bullets}
    edits[bullets[0]["id"]] = ["First half of a split bullet.", "Second half of a split bullet."]
    if doc.blocks and doc.blocks[0]["kind"] == "summary":
        edits["S"] = ["A rewritten summary line."]
    if len(doc.skills) >= 2:
        edits[doc.skills[0]["id"]], edits[doc.skills[1]["id"]] = [doc.skills[1]["text"]], [doc.skills[0]["text"]]
    groups: dict[str, list[str]] = {}
    for b in bullets:
        groups.setdefault(b["group"], []).append(b["id"])
    order = [list(reversed(ids)) for ids in groups.values() if len(ids) >= 2][:1]
    names = [n for n, _, _ in doc.sections()]
    section_order = names[:-2] + [names[-1], names[-2]] if len(names) >= 3 else None

    out = doc.render(edits, order=order, section_order=section_order)
    assert RF.from_engine(out, text) == out
    after = resume_pdf.parse(out)
    split_role = next(e for s in after.sections for e in s.entries
                      if "First half of a split bullet." in e.bullets)
    i = split_role.bullets.index("First half of a split bullet.")
    assert split_role.bullets[i + 1] == "Second half of a split bullet."
    assert sum(len(e.bullets) for s in after.sections for e in s.entries) == len(bullets) + 1


def test_the_format_round_trip_is_lossless():
    _check_edits_survive(FORMAT_TEXT, _check_lossless(FORMAT_TEXT))


def test_a_created_summary_lands_as_a_format_section():
    no_summary = FORMAT_TEXT.replace(
        "## SUMMARY\nAI engineer who ships tested agent systems for small businesses.\n", "")
    doc = _check_lossless(no_summary)
    assert doc.new_summary is not None
    out = doc.render({"S": ["AI engineer who ships tested agent systems."]})
    parsed = resume_pdf.parse(RF.from_engine(out, no_summary))
    assert parsed.sections[0].title == "SUMMARY"
    assert parsed.sections[0].paragraphs == ["AI engineer who ships tested agent systems."]


@pytest.mark.parametrize(
    "old,new,what",
    [
        ("### Acme Robotics, Austin, TX | 2024 to present", "### Acme Robotics, Austin, TX | 2022 to present", "EXPERIENCE"),
        ("Austin, TX | 555-0100", "Austin, TX | 555-0199", "the header"),
        ("Associate of Applied Science, Computer Science", "Bachelor of Science, Computer Science", "EDUCATION"),
        ("Senior AI Engineer\n", "Staff AI Engineer\n", "EXPERIENCE"),
    ],
)
def test_an_engine_result_whose_frame_drifted_is_refused(old, new, what):
    assert old in FORMAT_TEXT
    with pytest.raises(RF.FormatMismatch, match=what):
        RF.from_engine(FORMAT_TEXT.replace(old, new), FORMAT_TEXT)


def test_an_engine_result_that_does_not_parse_is_refused():
    with pytest.raises(RF.FormatMismatch, match="does not parse"):
        RF.from_engine("no name line at all", FORMAT_TEXT)


def test_text_the_engine_would_misread_is_refused():
    # A role line that is a known section name: the engine would read it as a section.
    misread = FORMAT_TEXT.replace("### DocChat, RAG over PDFs | 2025", "### Projects")
    with pytest.raises(RF.FormatMismatch, match="reads this role line as a section"):
        RF.to_engine(misread)
    with pytest.raises(RF.FormatMismatch, match="notes block"):
        RF.to_engine("Changed: the summary.\n" + FORMAT_TEXT)


# ------------------------------------------------------------------ the real master

MASTER_DIR = Path(os.environ.get("ATLAS_MASTER_RESUME_DIR") or jobs_config.RESUME_DIR)
_has_master = MASTER_DIR.is_dir() and any(
    f.suffix.lower() in (".docx", ".md", ".txt") for f in MASTER_DIR.iterdir())
needs_master = pytest.mark.skipif(not _has_master, reason="no master resume on this machine")


@needs_master
def test_the_master_resume_round_trips_losslessly():
    text = RF.master_format(MASTER_DIR)
    _check_edits_survive(text, _check_lossless(text))


@needs_master
def test_the_master_gives_the_engine_something_to_edit():
    doc = E.Doc(RF.master_format(MASTER_DIR))
    kinds = [b["kind"] for b in doc.editable()]
    assert kinds.count("summary") == 1 and kinds.count("bullet") >= 5 and kinds.count("skills") >= 1
