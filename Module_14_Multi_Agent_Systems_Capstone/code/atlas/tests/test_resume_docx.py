"""The resume loader reads .docx without a new dependency."""

from __future__ import annotations

import zipfile

from core.jobs.config import resume_text

DOC = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    "<w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>AI </w:t></w:r><w:r><w:t>engineer</w:t></w:r></w:p>"
    "<w:p/></w:body></w:document>"
)


def test_docx_resume_is_read(tmp_path):
    with zipfile.ZipFile(tmp_path / "resume.docx", "w") as z:
        z.writestr("word/document.xml", DOC)
    assert resume_text(tmp_path) == "Jane Doe\nAI engineer"


def test_unreadable_docx_is_skipped(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    (tmp_path / "ok.md").write_text("fallback", encoding="utf-8")
    assert resume_text(tmp_path) == "fallback"
