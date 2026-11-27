"""Tailored resume PDFs (core/jobs/resume_pdf.py), the career agent's
one-page gate, and the download route.

Rendering is pure; the route and agent-tool tests use the live test
database and remove applications whose company starts with 'Testco'.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import pytest_asyncio
from pypdf import PdfReader

from agents.career import tools as career_tools
from core import db
from core.jobs import resume_pdf, store
from core.jobs.config import resume_text
from core.web.api import app

RESUME = """Changed: led with the voice agent for this role.
Cut the trading project to make room.
# Jane Doe
Houston, TX | 713-555-0100 | jane@example.com | github.com/jane
## Summary
AI engineer who ships agentic systems to production for real businesses.
## Skills
- **Agentic development:** tool use, approval gates, evals.
- **Platforms:** Python, FastAPI, Postgres.
## Experience
### Acme Consulting, Houston, TX | 2026 to present
AI Engineer
- Shipped an inventory system used daily by a surgical practice.
- 375 unit and 168 integration tests against real Postgres.
### Example Property Group | Apr 2016 to present
- Automated investor correspondence behind human review.
## Education
### Houston Community College | Expected Dec 2026
Associate of Applied Science, Artificial Intelligence
"""


def _text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


# ------------------------------------------------------------------ parse


def test_parse_reads_every_part_and_keeps_the_note_out():
    r = resume_pdf.parse(RESUME)
    assert r.name == "Jane Doe"
    assert r.contact == ["Houston, TX", "713-555-0100", "jane@example.com", "github.com/jane"]
    assert r.notes.startswith("Changed:") and "Cut the trading" in r.notes
    assert [s.title for s in r.sections] == ["Summary", "Skills", "Experience", "Education"]
    skills, exp = r.sections[1], r.sections[2]
    assert skills.bullets[0].startswith("**Agentic development:**")
    acme = exp.entries[0]
    assert (acme.left, acme.right, acme.subtitle) == ("Acme Consulting, Houston, TX", "2026 to present", "AI Engineer")
    assert len(acme.bullets) == 2
    falls = exp.entries[1]
    assert falls.subtitle == "" and falls.bullets == ["Automated investor correspondence behind human review."]
    assert r.sections[3].entries[0].subtitle.startswith("Associate")


@pytest.mark.parametrize("text,fix", [
    ("", "# Full Name"),
    ("just prose", "# Full Name"),
    ("# \n## Summary\nx", "name"),
    ("# Jane\nHouston", "## Section"),
    ("# Jane\n## Summary\n## Skills\n- x", "'Summary' section is empty"),
    ("# Jane\n- a bullet before any section", "contact details"),
])
def test_parse_errors_say_what_to_fix(text, fix):
    with pytest.raises(resume_pdf.ResumeFormatError, match=fix):
        resume_pdf.parse(text)


def test_the_documented_format_parses():
    r = resume_pdf.parse(resume_pdf.FORMAT)
    assert r.name == "Full Name" and len(r.sections) == 3


# ------------------------------------------------------------------ render


def test_render_fits_one_page_at_the_largest_size_with_real_text():
    out = resume_pdf.render(RESUME)
    assert out.fits and out.pages == 1 and out.font_size == resume_pdf.SIZES[0]
    assert out.pdf.startswith(b"%PDF")
    text = _text(out.pdf)
    for needle in ("Jane Doe", "EXPERIENCE", "Acme Consulting", "2026 to present", "Agentic development:"):
        assert needle in text
    assert "Changed:" not in text and "**" not in text  # note and markup never print
    meta = PdfReader(io.BytesIO(out.pdf)).metadata
    assert meta.title == "Jane Doe - Resume" and meta.author == "Jane Doe"


def test_render_shrinks_type_before_giving_up_then_reports_pages():
    bullet = "- " + "Delivered a measurable outcome for a real client with tests and review. " * 2
    medium = RESUME + "## Projects\n### Side project | 2026\n" + "\n".join([bullet] * 16) + "\n"
    out = resume_pdf.render(medium)
    assert out.fits and out.font_size < resume_pdf.SIZES[0]

    huge = RESUME + "## Projects\n### Side project | 2026\n" + "\n".join([bullet] * 80) + "\n"
    out = resume_pdf.render(huge)
    assert not out.fits and out.pages >= 2 and out.font_size == resume_pdf.SIZES[-1]


def test_characters_the_pdf_fonts_lack_are_substituted_not_boxed():
    text = RESUME.replace("Shipped an inventory", "Cut errors 40% → 0 ≥ goal ★ ✓ Shipped an inventory")
    out = resume_pdf.render(text)
    extracted = _text(out.pdf)
    assert "40% -> 0 >= goal *" in extracted
    assert resume_pdf._clean("café – “quotes”") == "café – “quotes”"  # Windows-1252 survives


def test_markup_escapes_html_and_allows_bold():
    assert resume_pdf._markup("a < b & **c**") == "a &lt; b &amp; <b>c</b>"


def test_filename_is_safe_for_a_header():
    r = resume_pdf.parse(RESUME)
    assert resume_pdf.filename(r, "Acme, Inc.") == "Jane Doe - Resume - Acme, Inc..pdf"
    assert resume_pdf.filename(r, 'Evil"\r\nX-Header: 1') == "Jane Doe - Resume - EvilX-Header 1.pdf"
    assert resume_pdf.filename(r) == "Jane Doe - Resume.pdf"


# ------------------------------------------------------------------ docx tabs


def test_docx_tab_becomes_a_separator(tmp_path):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (
        f"<w:document {w}><w:body>"
        "<w:p><w:r><w:t>Acme, Houston, TX</w:t></w:r><w:r><w:tab/><w:t>2026 to present</w:t></w:r></w:p>"
        "<w:p><w:r><w:tab/><w:t>Indented line</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Trailing</w:t><w:tab/><w:tab/></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(tmp_path / "r.docx", "w") as z:
        z.writestr("word/document.xml", doc)
    assert resume_text(tmp_path) == "Acme, Houston, TX | 2026 to present\nIndented line\nTrailing"


# ------------------------------------------------------------------ agent gate and route

aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module")
async def application():
    row = await store.upsert_application("Testco Resume", "AI Engineer", status="interested", source="manual")
    yield row
    await db.execute("delete from job_applications where company like 'Testco%'")


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


@aio
async def test_agent_cannot_save_a_resume_that_will_not_render(application):
    save = career_tools._save_materials
    bad = await save({"application_id": str(application["id"]), "kind": "resume", "text": "no name line"})
    assert "error" in bad and "# Full Name" in bad["error"] and "## Experience" in bad["error"]

    bullet = "- " + "Delivered a measurable outcome for a real client with tests and review. " * 2
    long = RESUME + "## Projects\n### P | 2026\n" + "\n".join([bullet] * 80)
    too_long = await save({"application_id": str(application["id"]), "kind": "resume", "text": long})
    assert "error" in too_long and "pages" in too_long["error"] and "one page" in too_long["error"]
    assert not ((await store.get_application(application["id"]))["materials"] or {}).get("resume")

    ok = await save({"application_id": str(application["id"]), "kind": "resume", "text": RESUME})
    assert ok == {"saved": "resume", "application_id": str(application["id"])}
    # Other kinds are not run through the resume renderer.
    note = await save({"application_id": str(application["id"]), "kind": "cover", "text": "Plain prose."})
    assert note.get("saved") == "cover"


@aio
async def test_route_serves_the_pdf_or_says_why_not(application, client):
    url = f"/api/jobs/applications/{application['id']}/resume.pdf"
    r = await client.get(url)
    assert r.status_code == 404 and "draft one first" in r.json()["detail"]

    await store.save_materials(application["id"], {"resume": {"text": "free prose, no format", "at": "x"}})
    r = await client.get(url)
    assert r.status_code == 422 and "# Full Name" in r.json()["detail"]

    await store.save_materials(application["id"], {"resume": {"text": RESUME, "at": "x"}})
    r = await client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["x-resume-pages"] == "1"
    assert r.headers["content-disposition"] == 'inline; filename="Jane Doe - Resume - Testco Resume.pdf"'
    assert "Acme Consulting" in _text(r.content)

    assert (await client.get("/api/jobs/applications/not-a-uuid/resume.pdf")).status_code == 404
