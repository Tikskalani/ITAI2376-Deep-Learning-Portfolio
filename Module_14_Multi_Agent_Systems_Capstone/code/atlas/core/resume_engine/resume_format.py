"""The seam between resume_pdf's format and the APEX engine's text.

Tailored resumes in Atlas are written in core.jobs.resume_pdf.FORMAT ("# Name",
"## Section", "### Company | dates", "- bullet"): precheck, the PDF renderer and the
submitter all read that format. The engine reads plain resume text but already
understands markdown headers and keeps each bullet's marker when it rewrites a
line, so FORMAT text goes into the engine as it is and comes back in FORMAT.
to_engine() and from_engine() are therefore identities on the text, but they are
the checked boundary: to_engine() refuses a resume the engine would misread (a
bullet it would not edit, a role heading it would take for a section), and
from_engine() refuses an engine result whose structure drifted (a role heading,
the contact block or a protected section that changed). Both fail closed.

The master resume is a .docx in data/resume/. docx_to_format() converts it
deterministically from the document's own structure: list paragraphs become
bullets, a line with a tab (company, then dates at the right margin) becomes a
"###" role line, short all-caps lines become sections. No model is involved.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from defusedxml import ElementTree as ET

from core.jobs import resume_pdf
from core.resume_engine import apex_engine as E

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class FormatMismatch(ValueError):
    """The text cannot cross the seam safely. `problems` says why, one line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# ------------------------------------------------------------------ the master


def _paragraphs(path: Path) -> list[tuple[str, bool, bool]]:
    """(text, is_list_item, has_tab) per non-empty paragraph of a .docx.

    Text follows jobs_config._docx_text: a tab becomes " | ", trailing tabs and
    runs of them collapse, so the role line reads "Company | 2026 to present".
    """
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{_W}p"):
        ppr = p.find(f"{_W}pPr")
        style = ppr.find(f"{_W}pStyle") if ppr is not None else None
        listed = ppr is not None and (
            ppr.find(f"{_W}numPr") is not None
            or (style is not None and style.get(f"{_W}val") == "ListParagraph")
        )
        parts: list[str] = []
        tabbed = False
        for el in p.iter():
            if el.tag == f"{_W}t":
                parts.append(el.text or "")
            elif el.tag in (f"{_W}tab", f"{_W}ptab") and "".join(parts).strip():
                parts.append(" | ")
                tabbed = True
        line = re.sub(r"(\s*\|\s*)+$", "", "".join(parts))
        line = re.sub(r"(\s*\|\s*){2,}", " | ", line)
        if line.strip():
            out.append((line, listed, tabbed and " | " in line))
    return out


def docx_to_format(path: Path) -> str:
    """The .docx resume in resume_pdf.FORMAT. Raises FormatMismatch when the
    document has no shape this can read (no name, no section headers)."""
    paras = _paragraphs(path)
    if not paras:
        raise FormatMismatch(["the .docx has no text"])
    lines = ["# " + paras[0][0].strip()]
    in_section = False
    for text, listed, tabbed in paras[1:]:
        s = text.strip()
        if listed:
            if not in_section:
                raise FormatMismatch([f"a bullet before the first section: {s[:60]!r}"])
            lines.append("- " + s)
        elif not tabbed and E.is_header(s):
            lines.append("## " + s)
            in_section = True
        elif tabbed and in_section:
            lines.append("### " + s)
        else:
            lines.append(s)
    if not in_section:
        raise FormatMismatch(["no section headers found (SUMMARY, EXPERIENCE, ...)"])
    return "\n".join(lines)


def master_format(directory: Path | None = None) -> str:
    """The newest master in data/resume/ in FORMAT: a .docx is converted, a .md
    or .txt must already be in FORMAT. Raises FormatMismatch otherwise."""
    from core.jobs import config as jobs_config

    d = directory or jobs_config.RESUME_DIR
    files = sorted(
        (f for f in d.iterdir() if f.suffix.lower() in (".md", ".txt", ".docx")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    ) if d.is_dir() else []
    if not files:
        raise FormatMismatch([f"no .docx, .md or .txt master resume in {d}"])
    f = files[0]
    text = docx_to_format(f) if f.suffix.lower() == ".docx" else f.read_text(encoding="utf-8")
    try:
        resume_pdf.parse(text)
    except resume_pdf.ResumeFormatError as exc:
        raise FormatMismatch([f"{f.name} is not in the resume format: {exc}"]) from exc
    return text


# ------------------------------------------------------------------ the seam


def _is_header_line(line: str) -> bool:
    return line.startswith("## ") or line == "##"


def _layout(text: str) -> list[tuple[str, str, str]]:
    """(kind, section, entry) per line of FORMAT text, as resume_pdf reads it.
    kind: name, contact, section, entry, subtitle, bullet, paragraph, blank, notes."""
    out: list[tuple[str, str, str]] = []
    seen_name = False
    section = entry = ""
    subtitle_open = False
    for raw in text.split("\n"):
        ln = raw.strip()
        if not seen_name:
            if ln == "#" or ln.startswith("# "):
                seen_name = True
                out.append(("name", "", ""))
            else:
                out.append(("notes", "", ""))
            continue
        if not ln:
            out.append(("blank", section, entry))
        elif _is_header_line(ln):
            section, entry, subtitle_open = ln[3:].strip(), "", False
            out.append(("section", section, ""))
        elif not section:
            out.append(("contact", "", ""))
        elif ln.startswith("### "):
            entry, subtitle_open = ln, True
            out.append(("entry", section, entry))
        elif ln.startswith(("- ", "* ", "• ")):
            subtitle_open = False
            out.append(("bullet", section, entry))
        elif entry and subtitle_open:
            subtitle_open = False
            out.append(("subtitle", section, entry))
        else:
            out.append(("paragraph", section, entry))
    return out


def alignment_problems(text: str) -> list[str]:
    """Where the engine would read FORMAT text differently from resume_pdf.
    Empty means every bullet the format shows in an editable section is one
    engine bullet in the right role, every role line is a role heading, the
    summary is the engine's summary, and nothing protected is editable."""
    problems: list[str] = []
    lines = text.split("\n")
    layout = _layout(text)
    doc = E.Doc(text)
    block_at = {b["start"]: b for b in doc.blocks + doc.skills}
    covered = {i for b in doc.blocks + doc.skills for i in range(b["start"], b["end"])}

    for i, (kind, section, entry) in enumerate(layout):
        editable = bool(section) and bool(E.EDITABLE_SECTIONS.search(section))
        skills = bool(section) and bool(E.SKILL_SECTIONS.search(section)) and not editable
        summary = bool(section) and bool(E.SUMMARY_SECTIONS.match(section))
        b = block_at.get(i)
        where = f"line {i + 1}"
        if kind == "bullet" and editable:
            if not b or b["kind"] != "bullet" or b["end"] != i + 1:
                problems.append(f"{where}: a bullet the engine would not edit on its own")
            elif entry and not b["group"].startswith(entry):
                problems.append(f"{where}: the engine puts this bullet under the wrong role")
        elif kind in ("entry", "subtitle", "name", "contact", "section"):
            if i in covered:
                problems.append(f"{where}: a {kind} line the engine would rewrite")
            if kind == "section" and doc.section_of[i][0] != "#header":
                problems.append(f"{where}: the engine does not see this section header")
            if kind == "entry" and doc.section_of[i][0] == "#header":
                problems.append(f"{where}: the engine reads this role line as a section")
        elif kind == "paragraph" and skills:
            if not b or b["kind"] != "skills":
                problems.append(f"{where}: a skills line the engine would not see")
        elif kind == "paragraph" and summary:
            if i not in covered:
                problems.append(f"{where}: summary text outside the engine's summary")
        elif kind in ("bullet", "paragraph") and not editable and not skills and not summary:
            if i in covered:
                problems.append(f"{where}: a protected line the engine would edit")
    if any(lines[i].strip() for i, (k, _, _) in enumerate(layout) if k == "notes"):
        problems.append("text before '# Name' (a notes block) must be removed before the engine reads it")
    return problems


def to_engine(format_text: str) -> str:
    """FORMAT text as the engine's input. Raises FormatMismatch if resume_pdf
    cannot parse it or the engine would read it differently."""
    text = (format_text or "").replace("\r\n", "\n")
    try:
        resume_pdf.parse(text)
    except resume_pdf.ResumeFormatError as exc:
        raise FormatMismatch([str(exc)]) from exc
    problems = alignment_problems(text)
    if problems:
        raise FormatMismatch(problems)
    return text


def _frame(text: str) -> list[tuple[str, str]]:
    """The lines the engine must never change: name, contact, section and role
    lines and role subtitles, plus every line of a protected section. Each as
    (kind, line). Section order may change; this is compared per section."""
    out = []
    for line, (kind, section, _) in zip(text.split("\n"), _layout(text), strict=True):
        editable = bool(section) and (
            bool(E.EDITABLE_SECTIONS.search(section))
            or bool(E.SKILL_SECTIONS.search(section))
            or bool(E.SUMMARY_SECTIONS.match(section))
        )
        if kind in ("name", "contact", "section", "entry", "subtitle") or (
            kind in ("bullet", "paragraph") and section and not editable
        ):
            out.append((section if kind != "section" else line, line))
    return out


def from_engine(engine_text: str, source_format: str) -> str:
    """The engine's result back in FORMAT, checked against the FORMAT source.
    Raises FormatMismatch if it does not parse or its frame changed."""
    text = (engine_text or "").replace("\r\n", "\n")
    try:
        resume_pdf.parse(text)
    except resume_pdf.ResumeFormatError as exc:
        raise FormatMismatch([f"the engine's text does not parse: {exc}"]) from exc

    def per_section(frame: list[tuple[str, str]]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for key, line in frame:
            groups.setdefault(key, []).append(line)
        return groups

    before, after = per_section(_frame(source_format)), per_section(_frame(text))
    # The one structural addition the engine makes: a summary section on a
    # resume that had none (its header line is the only fixed line it adds).
    for key in set(after) - set(before):
        if after[key] == [key] and E.SUMMARY_SECTIONS.match(key[3:].strip()):
            del after[key]
    problems = [
        f"{key or 'the header'}: its fixed lines changed"
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]
    if problems:
        raise FormatMismatch(problems)
    return text


__all__ = [
    "FormatMismatch",
    "alignment_problems",
    "docx_to_format",
    "from_engine",
    "master_format",
    "to_engine",
]
