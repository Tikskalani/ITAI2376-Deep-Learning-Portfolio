"""The approved one-page master on Trilok's laptop: it renders on one page, the
bullets it cut come back as evidence for their own role, and editing the full
master invalidates the selection. Skips where data/resume/ has no master and
no approved selection (CI, and any machine but his). The edit case works on a
copy of the .docx in a temp folder; the real file is only read.
"""

from __future__ import annotations

import shutil
import zipfile

import pytest

from core.jobs import resume_pdf
from core.resume_engine import apex_engine as E
from core.resume_engine import one_page
from core.resume_engine import resume_format as RF
from tests.test_resume_format import MASTER_DIR, _has_master

needs_selection = pytest.mark.skipif(
    not (_has_master and (MASTER_DIR / one_page.SELECTION_NAME).is_file()),
    reason="no master resume with an approved one-page selection on this machine",
)


@needs_selection
def test_the_one_page_master_renders_on_one_page():
    full = RF.master_format(MASTER_DIR)
    text = one_page.load(full, MASTER_DIR)
    assert text is not None
    assert resume_pdf.render(text).pages == 1
    assert RF.alignment_problems(text) == [], "the engine reads the one-page text cleanly"
    assert RF.from_engine(text, full) == text, "cutting bullets changed no fixed line of the master"


@needs_selection
def test_every_cut_bullet_comes_back_as_evidence_for_its_own_role():
    full = RF.master_format(MASTER_DIR)
    text = one_page.load(full, MASTER_DIR)
    assert text is not None
    facts = one_page.evidence(full, text)
    full_doc, page_doc = E.Doc(full), E.Doc(text)
    cut = [b for b in full_doc.blocks if b["kind"] == "bullet" and b["text"] not in
           {x["text"] for x in page_doc.blocks}]
    assert cut, "the selection cut something"
    for b in cut:
        mine = [f for f in facts if f["text"] in b["text"]]
        assert mine and " ".join(f["text"] for f in mine).replace(" ", "") == b["text"].replace(" ", ""), \
            f"{b['id']} comes back whole"
        assert {f["topic"] for f in mine} == {b["group"].split(" | ")[0].removeprefix("### ")}

    page_doc.attach_facts(facts)
    for b in cut:
        role = b["group"].split(" | ")[0]
        bullets_of_role = [x for x in page_doc.blocks if x["kind"] == "bullet" and x["group"].startswith(role)]
        if not bullets_of_role:
            continue  # a role cut whole backs only the summary
        assert b["text"][:80] in page_doc.group_text(bullets_of_role[0]["id"]), \
            f"{b['id']} is evidence for its own role"
        others = [x for x in page_doc.blocks if x["kind"] == "bullet" and not x["group"].startswith(role)]
        for x in others:
            assert b["text"][:80] not in page_doc.group_text(x["id"]), f"{b['id']} leaked into another role"
        assert b["text"][:80] in page_doc.evidence_text(), "and for the summary"


@needs_selection
def test_editing_the_full_master_invalidates_the_selection(tmp_path):
    docx = next(f for f in sorted(MASTER_DIR.iterdir()) if f.suffix.lower() == ".docx")
    shutil.copy(docx, tmp_path / docx.name)
    shutil.copy(MASTER_DIR / one_page.SELECTION_NAME, tmp_path / one_page.SELECTION_NAME)
    full = RF.master_format(tmp_path)
    assert one_page.load(full, tmp_path) is not None, "the copy loads before any edit"

    edited = tmp_path / "edited" / docx.name
    edited.parent.mkdir()
    with zipfile.ZipFile(docx) as src, zipfile.ZipFile(edited, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                data = data.replace(b"Ten years running business operations",
                                    b"Eleven years running business operations", 1)
                assert b"Eleven years" in data
            dst.writestr(item, data)
    shutil.copy(MASTER_DIR / one_page.SELECTION_NAME, edited.parent / one_page.SELECTION_NAME)
    with pytest.raises(one_page.OnePageError, match="master resume changed"):
        one_page.load(RF.master_format(edited.parent), edited.parent)
