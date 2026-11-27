"""Procedures live in two roots: tracked (curated) and runtime (gitignored)."""

from __future__ import annotations

import pytest

from core.config import settings
from core.memory import procedural


@pytest.fixture
def roots(tmp_path, monkeypatch):
    tracked, runtime = tmp_path / "tracked", tmp_path / "runtime"
    for root, rel in ((tracked, "cost/a.md"), (runtime, "agents/b.md")):
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_text(f"# {rel}", encoding="utf-8")
    monkeypatch.setattr(settings, "procedures_dir", tracked)
    monkeypatch.setattr(settings, "runtime_procedures_dir", runtime)
    return tracked, runtime


def test_files_come_from_both_roots(roots):
    tracked, runtime = roots
    found = {(base, p.relative_to(base).as_posix()) for base, p in procedural._procedure_files()}
    assert found == {(tracked, "cost/a.md"), (runtime, "agents/b.md")}


async def test_load_reads_either_root(roots):
    assert await procedural.load_procedure("cost/a.md") == "# cost/a.md"
    assert await procedural.load_procedure("agents/b.md") == "# agents/b.md"
    with pytest.raises(FileNotFoundError):
        await procedural.load_procedure("nope/c.md")


@pytest.fixture
def secret(roots, tmp_path):
    """A file outside both roots that a traversal would try to read."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret", encoding="utf-8")
    (tmp_path / "loot.md").write_text("outside the roots", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "../.env",
        "..\\.env",
        "../loot.md",
        "..\\loot.md",
        "cost/../../loot.md",  # looks valid, climbs out
        "cost/../../.env",
        "agents/../../../loot.md",
        "",
        "   ",
        "cost/a.md\x00.md",
        "cost/bad<>|?*.md",  # not a legal Windows name
        "C:.env",
    ],
)
async def test_load_refuses_paths_outside_the_roots(secret, path):
    with pytest.raises(FileNotFoundError):
        await procedural.load_procedure(path)


async def test_load_refuses_absolute_paths_outside_the_roots(secret):
    for target in (secret / ".env", secret / "loot.md"):
        with pytest.raises(FileNotFoundError):
            await procedural.load_procedure(str(target))
        with pytest.raises(FileNotFoundError):
            await procedural.load_procedure(target.as_posix())


async def test_load_refuses_non_markdown_inside_a_root(roots):
    tracked, _ = roots
    (tracked / "cost" / "notes.txt").write_text("not a procedure", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        await procedural.load_procedure("cost/notes.txt")


async def test_load_refuses_a_symlink_out_of_the_root(roots, secret):
    tracked, _ = roots
    link = tracked / "cost" / "escape.md"
    try:
        link.symlink_to(secret / "loot.md")
    except OSError:
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(FileNotFoundError):
        await procedural.load_procedure("cost/escape.md")


async def test_load_accepts_an_absolute_path_inside_a_root(roots):
    """Older skills rows stored the absolute path; it still loads when it
    lands inside a root."""
    tracked, runtime = roots
    assert await procedural.load_procedure(str(tracked / "cost" / "a.md")) == "# cost/a.md"
    assert await procedural.load_procedure(str(runtime / "agents" / "b.md")) == "# agents/b.md"


async def test_load_accepts_dot_segments_that_stay_inside(roots):
    assert await procedural.load_procedure("cost/../cost/a.md") == "# cost/a.md"
    assert await procedural.load_procedure("./agents/b.md") == "# agents/b.md"


async def test_agent_tool_and_mcp_refuse_traversal(secret):
    from agents.cto.tools import _load_procedure
    from core.mcp_server import get_procedure

    out = await _load_procedure({"path": "../.env"})
    assert "error" in out and "sk-secret" not in str(out)
    ok = await _load_procedure({"path": "cost/a.md"})
    assert ok["content"] == "# cost/a.md"

    text = await get_procedure("../.env")
    assert "sk-secret" not in text and text.startswith("no procedure")


def test_runtime_dir_is_gitignored():
    from core.config import REPO_ROOT

    assert settings.runtime_procedures_dir.is_relative_to(REPO_ROOT / "data")
