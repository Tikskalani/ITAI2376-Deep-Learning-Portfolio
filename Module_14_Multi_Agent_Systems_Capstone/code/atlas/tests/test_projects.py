"""Tests for core/projects.py against the live test database.

Every row these tests create carries the 'test-' slug prefix and is
removed afterwards, so the suite leaves no projects behind.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import pytest_asyncio

from core import db, projects

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _cleanup():
    yield
    await db.execute("delete from projects where slug like 'test-%'")


async def test_upsert_creates_then_updates_without_clearing_loop_fields():
    p = await projects.upsert_project(
        "test-dental", "Demo Dental", kind="client", priority=1,
        repos=["atlas-dev/Demo-Implant-Portal"],
    )
    assert p.slug == "test-dental"
    assert p.repos == ["atlas-dev/demo-implant-portal"]  # normalised

    await projects.set_next_action(p.id, "Send the Implant invoice", date(2026, 10, 10))
    again = await projects.upsert_project(
        "test-dental", "Demo Dental (Implant)", kind="client", priority=2
    )
    assert again.id == p.id
    assert again.name == "Demo Dental (Implant)"
    assert again.priority == 2
    # The loop owns next_action; a re-seed must not wipe it.
    assert again.next_action == "Send the Implant invoice"
    assert again.next_action_due == date(2026, 10, 10)


async def test_validation_rejects_bad_kind_status_priority():
    with pytest.raises(ValueError):
        await projects.upsert_project("test-x", "x", kind="nonsense")
    with pytest.raises(ValueError):
        await projects.upsert_project("test-x", "x", status="nonsense")
    with pytest.raises(ValueError):
        await projects.upsert_project("test-x", "x", priority=9)


async def test_list_orders_by_priority_and_filters_status():
    await projects.upsert_project("test-b", "B", priority=3)
    await projects.upsert_project("test-a", "A", priority=1)
    await projects.upsert_project("test-c", "C", priority=2, status="parked")
    active = [p.slug for p in await projects.list_projects("active") if p.slug.startswith("test-")]
    assert active == ["test-a", "test-b"]
    everything = [p.slug for p in await projects.list_projects(None) if p.slug.startswith("test-")]
    assert everything == ["test-a", "test-c", "test-b"]


async def test_project_for_repo_is_case_insensitive():
    await projects.upsert_project(
        "test-hvac", "Demo HVAC", repos=["atlas-dev/hvac-site"]
    )
    found = await projects.project_for_repo("atlas-dev/HVAC-Site")
    assert found is not None and found.slug == "test-hvac"
    assert await projects.project_for_repo("nobody/nothing") is None


async def test_touch_updates_signal_and_touched_timestamps():
    p = await projects.upsert_project("test-touch", "Touch")
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    await projects.touch_project(p.id, signal_at=when)
    again = await projects.get_project("test-touch")
    assert again is not None
    assert again.last_signal_at == when
    assert again.last_touched_at is not None


def test_format_projects_block_is_compact_and_stable():
    rows = [
        projects.Project(
            id="00000000-0000-0000-0000-000000000001", slug="dental", name="Demo Dental",
            kind="client", status="active", priority=1, objective="Ship the portal",
            next_action="Invoice", next_action_due=date(2026, 10, 10),
            repos=["atlas-dev/demo-implant-portal"],
        ),
        projects.Project(
            id="00000000-0000-0000-0000-000000000002", slug="family", name="Family",
            kind="personal", status="active", priority=5,
        ),
    ]
    block = projects.format_projects_block(rows)
    assert block.startswith("- dental: Demo Dental [p1 | client | active | repos: atlas-dev/demo-implant-portal]")
    assert "objective: Ship the portal" in block
    assert "next: Invoice (due 2026-10-10)" in block
    assert "- family: Family [p5 | personal | active]" in block
    assert projects.format_projects_block([]) == "(no projects on record)"
