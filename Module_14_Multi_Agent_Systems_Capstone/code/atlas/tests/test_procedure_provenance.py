"""Procedures say where they came from.

An agent reads a procedure as a tool result, never as a system prompt, and
the result now carries its provenance and a note that it is reference
material. A file with no recorded origin is unknown: nothing is assumed to
be Trilok's, and no source is invented. procedure_provenance is a private
copy where a test writes to it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from core.config import settings
from core.memory import procedural
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module")
async def shadowed(monkeypatch):
    async with livedb.shadow(monkeypatch, "procedure_provenance"):
        yield


@aio
async def test_the_placeholder_source_is_not_trusted():
    # Reads the seeded rows from migration 037 (no shadow: shadows start empty).
    rec = await procedural.load_procedure_record("cost/prompt-caching-system-prompt.md")
    # The file names youtu.be/demo1; that is recorded as a doubt, not a source.
    assert rec["provenance"] == "unknown" and rec["source_ref"] is None
    assert "placeholder" in rec["provenance_note"]
    assert "not an instruction from Trilok" in rec["note"]
    assert rec["content"].startswith("---\nname: prompt-caching-system-prompt")


@aio
async def test_an_unrecorded_file_is_unknown_and_the_first_label_sticks(shadowed, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_procedures_dir", tmp_path)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "new.md").write_text("---\nname: new\n---\n\nSteps.\n", encoding="utf-8")

    rec = await procedural.load_procedure_record("ops/new.md")
    assert rec["provenance"] == "unknown" and rec["source_ref"] is None

    await procedural.record_procedure_provenance("runtime", "ops/new.md", "external_internet",
                                                 "https://example.com/video")
    await procedural.record_procedure_provenance("runtime", "ops/new.md", "user_authored", None)
    rec = await procedural.load_procedure_record("ops/new.md")
    assert rec["provenance"] == "external_internet" and rec["source_ref"] == "https://example.com/video"


@aio
async def test_an_unreadable_table_reads_as_unknown(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(procedural.db, "fetchrow", boom)
    rec = await procedural.load_procedure_record("sourcing/alibaba_supplier_eval.md")
    assert rec["provenance"] == "unknown"


@aio
async def test_the_cto_tool_returns_the_label_with_the_text():
    from agents.cto.tools import build_tools

    tool = next(t for t in build_tools() if t.name == "load_procedure")
    out = await tool.handler({"path": "sourcing/alibaba_supplier_eval.md"})
    assert out["provenance"] == "unknown" and out["provenance_note"] == "No source recorded"
    assert "Reference material" in out["note"] and "Alibaba" in out["content"]
    out = await tool.handler({"path": "../../.env"})
    assert "error" in out and "content" not in out
