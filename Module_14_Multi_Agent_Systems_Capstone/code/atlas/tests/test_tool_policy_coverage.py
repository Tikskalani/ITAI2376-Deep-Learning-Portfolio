"""Every tool Atlas ships has an explicit policy row.

The runtime refuses a tool no approval_policy row matches (core/runtime/
policy.py). That is only safe if every tool an agent can call is classified
on purpose, so these tests read the registry and fail when a tool, or a
server tool an agent opts into, has no exact row in migration 037. A new
tool therefore cannot ship until someone decides its gate.

The rows are read from the migration file itself (what deploy applies) and
from the migrated database (what the runtime reads).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.runtime import dispatcher, policy
from core.runtime.register_all import register_all

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"
# 037 classified every tool; later migrations add rows for the tools they bring (038: capabilities).
POLICY_MIGRATIONS = ("037_policy_provenance.sql", "038_capabilities.sql")
_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*'(auto|approve|deny)'\s*,")


def _seeded() -> dict[str, str]:
    rows: dict[str, str] = {}
    for name in POLICY_MIGRATIONS:
        sql = (MIGRATIONS / name).read_text(encoding="utf-8")
        block = sql[sql.index("insert into approval_policy"):sql.index("on conflict (tool_pattern)")]
        rows.update(_ROW.findall(block))
    return rows


def _registered() -> dict[str, set[str]]:
    """tool name -> agents that carry it, plus server:<name> for server tools."""
    register_all()
    out: dict[str, set[str]] = {}
    for name in dispatcher.registered_agents():
        reg = dispatcher.get_registration(name)
        for spec in reg.tools_factory():
            out.setdefault(spec.name, set()).add(name)
        for server in (reg.overrides or {}).get("server_tools") or []:
            out.setdefault(f"server:{server}", set()).add(name)
    return out


def test_every_registered_tool_has_an_exact_row():
    seeded = _seeded()
    missing = sorted(t for t in _registered() if t not in seeded)
    assert not missing, (
        f"no approval_policy row for {missing}: add one to a migration with a gate chosen "
        "from what the handler does; until then the runtime refuses the tool"
    )


def test_no_seeded_row_is_left_over():
    # A row for a tool that no longer exists is harmless but misleading.
    stale = sorted(set(_seeded()) - set(_registered()))
    assert not stale, f"rows for tools no agent has: {stale}"


def test_the_counts_the_report_gives():
    """One counting basis, said plainly: unique tool names (57 local, 59 with
    the two Anthropic server tools), and separately agent-tool registrations
    (a tool several agents carry counts once per agent: 107). 2026-10-24:
    the resume engine's two tools (#76) and the CTO's three capability tools (038)."""
    reg = _registered()
    local = {t for t in reg if not t.startswith("server:")}
    assert len(local) == 57 and len(reg) == 59
    assert sum(len(a) for t, a in reg.items() if t in local) == 107


@pytest.mark.parametrize("tool", ["dispatch_agent", "run_workflow", "queue_for_tonight",
                                  "draft_email", "import_statement", "calendar_create_event"])
def test_delegation_and_outward_tools_ask_first(tool):
    assert _seeded()[tool] == "approve"


async def test_the_migrated_database_classifies_every_tool():
    policy.invalidate_cache()
    for tool in _registered():
        gate, pattern = await policy.gate_for(tool)
        assert gate not in policy.REFUSING, f"{tool} would be refused: {gate} {pattern}"
        # The wildcard fences still win where they match: deny beats an exact auto.
        assert pattern is not None
