"""A tailored resume, as a typeset one-page PDF.

The career agent writes each tailored resume in a small structured format
(FORMAT below). This module parses it and renders a single-column,
ATS-friendly letter page: real selectable text, standard section headings,
no images or columns a parser could scramble. It tries a few type sizes and
keeps the largest that fits on one page; if even the smallest does not fit,
the result says how many pages it took so the agent can cut.

Anything before the "# Name" line is the agent's review note ("Changed: …")
and is returned separately, never printed.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

FORMAT = """\
# Full Name
City, ST | phone | email | github.com/user | linkedin.com/in/user
## Summary
One short paragraph.
## Experience
### Company, City, ST | 2026 to present
Role title (optional line right under the ### line)
- Bullet. **Bold** is allowed.
## Education
### School, City, ST | Expected Dec 2026
Degree"""

# Largest first: the first size that fits on one page wins.
SIZES = (10.5, 10.25, 10.0, 9.75, 9.5, 9.25, 9.0)


class ResumeFormatError(ValueError):
    """The text does not follow FORMAT. The message says what to fix."""


@dataclass
class Entry:
    left: str
    right: str = ""
    subtitle: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class Section:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Resume:
    name: str
    contact: list[str]
    sections: list[Section]
    notes: str = ""


@dataclass
class Rendered:
    pdf: bytes
    pages: int
    font_size: float

    @property
    def fits(self) -> bool:
        return self.pages == 1


def parse(text: str) -> Resume:
    """Parse the agent's resume text. Raises ResumeFormatError with a fix."""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    start = next((i for i, ln in enumerate(lines) if ln == "#" or ln.startswith("# ")), None)
    if start is None:
        raise ResumeFormatError("Start the resume with '# Full Name' on its own line.")
    notes = "\n".join(ln for ln in lines[:start] if ln.strip()).strip()
    name = lines[start][1:].strip()
    if not name:
        raise ResumeFormatError("The '# ' line needs the candidate's name.")

    contact: list[str] = []
    sections: list[Section] = []
    entry: Entry | None = None
    for raw in lines[start + 1:]:
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("## "):
            sections.append(Section(ln[3:].strip()))
            entry = None
        elif not sections:
            if ln.startswith(("#", "-", "*")):
                raise ResumeFormatError("Put contact details on plain lines right under the name, then '## ' sections.")
            contact += [part.strip() for part in ln.split("|") if part.strip()]
        elif ln.startswith("### "):
            left, _, right = ln[4:].partition("|")
            entry = Entry(left.strip(), right.strip())
            sections[-1].entries.append(entry)
        elif ln.startswith(("- ", "* ", "• ")):
            bullet = ln[2:].strip()
            (entry.bullets if entry else sections[-1].bullets).append(bullet)
        elif entry is not None and not entry.subtitle and not entry.bullets:
            entry.subtitle = ln
        elif entry is not None:
            entry.bullets.append(ln)  # a wrapped bullet the agent forgot to mark
        else:
            sections[-1].paragraphs.append(ln)
    if not sections:
        raise ResumeFormatError("Add at least one '## Section' (Summary, Experience, Education...).")
    for s in sections:
        if not (s.paragraphs or s.bullets or s.entries):
            raise ResumeFormatError(f"The '{s.title}' section is empty.")
    return Resume(name, contact, sections, notes)


_BOLD = re.compile(r"\*\*(.+?)\*\*")
# Characters the built-in PDF fonts lack, mapped to close equivalents.
_SUBSTITUTE = {"→": "->", "←": "<-", "≈": "~", "≥": ">=", "≤": "<=", "✓": "", "★": "*", " ": " "}


def _clean(text: str) -> str:
    """Fit text to the built-in fonts (Windows-1252), never a missing glyph box."""
    out = []
    for ch in text:
        ch = _SUBSTITUTE.get(ch, ch)
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            folded = unicodedata.normalize("NFKD", ch).encode("cp1252", "ignore").decode("cp1252")
            out.append(folded)
    return "".join(out)


def _markup(text: str) -> str:
    """Escape for reportlab's mini-HTML, then allow **bold**."""
    safe = _clean(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _BOLD.sub(r"<b>\1</b>", safe)


def _build(resume: Resume, size: float) -> tuple[bytes, int]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        KeepTogether,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    ink, muted, rule = colors.HexColor("#111111"), colors.HexColor("#444444"), colors.HexColor("#9a9a9a")
    lead = size * 1.28
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=size, leading=lead, textColor=ink)
    styles = {
        "name": ParagraphStyle("name", parent=body, fontName="Helvetica-Bold", fontSize=size * 1.9,
                               leading=size * 2.2, alignment=TA_CENTER),
        "contact": ParagraphStyle("contact", parent=body, fontSize=size * 0.92, leading=size * 1.2,
                                  alignment=TA_CENTER, textColor=muted),
        "head": ParagraphStyle("head", parent=body, fontName="Helvetica-Bold", fontSize=size * 0.98,
                               leading=size * 1.2, spaceBefore=size * 0.9),
        "left": ParagraphStyle("left", parent=body, fontName="Helvetica-Bold"),
        "right": ParagraphStyle("right", parent=body, alignment=TA_RIGHT, textColor=muted),
        "sub": ParagraphStyle("sub", parent=body, fontName="Helvetica-Oblique", textColor=muted),
        "bullet": ParagraphStyle("bullet", parent=body),
    }

    buf = io.BytesIO()
    margin = 0.55 * inch
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, leftMargin=margin, rightMargin=margin, topMargin=0.5 * inch,
        bottomMargin=0.5 * inch, title=_clean(f"{resume.name} - Resume"), author=_clean(resume.name),
        subject="Resume", creator="Atlas",
    )
    width = doc.width - 12  # the frame pads 6pt on each side; tables must match the text column

    def bullets(items: list[str]) -> ListFlowable:
        return ListFlowable(
            [ListItem(Paragraph(_markup(b), styles["bullet"]), leftIndent=size * 1.1, value="•") for b in items],
            bulletType="bullet", start="•", leftIndent=size * 1.1, bulletFontSize=size * 0.8,
            bulletOffsetY=-size * 0.05, spaceBefore=size * 0.15,
        )

    flow: list = [Paragraph(_markup(resume.name), styles["name"])]
    if resume.contact:
        flow.append(Paragraph(_markup("  |  ".join(resume.contact)), styles["contact"]))
    for s in resume.sections:
        head = [Paragraph(_markup(s.title.upper()), styles["head"]),
                HRFlowable(width="100%", thickness=0.6, color=rule, spaceBefore=1, spaceAfter=size * 0.35)]
        # Each part is a list of flowables that belong on one page together.
        parts: list[list] = [[Paragraph(_markup(p), body)] for p in s.paragraphs]
        if s.bullets:
            parts.append([bullets(s.bullets)])
        for i, e in enumerate(s.entries):
            row = Table(
                [[Paragraph(_markup(e.left), styles["left"]), Paragraph(_markup(e.right), styles["right"])]],
                colWidths=[width * 0.72, width * 0.28],
            )
            row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                     ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                     ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
            block: list = [Spacer(1, size * 0.45)] if i or parts else []
            block.append(row)
            if e.subtitle:
                block.append(Paragraph(_markup(e.subtitle), styles["sub"]))
            if e.bullets:
                block.append(bullets(e.bullets))
            parts.append(block)
        # The heading stays with the section's first part; every later part
        # stays whole. Never nest KeepTogether: reportlab then breaks pages
        # before every section.
        flow.append(KeepTogether(head + parts[0]))
        flow.extend(KeepTogether(p) for p in parts[1:])

    doc.build(flow)
    return buf.getvalue(), doc.page


def render(resume: Resume | str) -> Rendered:
    """The largest type size that fits one page, or the smallest size with
    the page count it needs."""
    parsed = parse(resume) if isinstance(resume, str) else resume
    pdf, pages = b"", 0
    for size in SIZES:
        pdf, pages = _build(parsed, size)
        if pages == 1:
            return Rendered(pdf, 1, size)
    return Rendered(pdf, pages, SIZES[-1])


def filename(resume: Resume, company: str | None = None) -> str:
    """'Trilok Kalani - Resume - Acme.pdf': what a recruiter sees in the ATS."""
    parts = [resume.name, "Resume"] + ([company] if company else [])
    safe = re.sub(r"[^\w .,&()-]+", "", " - ".join(_clean(p) for p in parts)).strip()
    return (safe or "Resume")[:120] + ".pdf"
