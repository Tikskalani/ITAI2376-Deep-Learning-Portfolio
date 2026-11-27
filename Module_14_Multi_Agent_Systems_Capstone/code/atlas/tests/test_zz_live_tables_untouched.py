"""The drift guard: no test may change a live table.

conftest.py hashes every public table when the session starts; this file is
collected last (alphabetically), so its first test compares once every other
test has run. A test that writes to a real table without tests/livedb.py's
shadow() fails here, by name. Until 2026-10-22 tests/test_job_answers.py did:
it put Trilok's answers back by delete and insert, content intact,
timestamps not.
"""

from __future__ import annotations

import os

import pytest

from tests import livedb
from tests.conftest import LIVE_BEFORE


def test_the_suite_left_every_live_table_untouched(request: pytest.FixtureRequest) -> None:
    before = request.config.stash.get(LIVE_BEFORE, None)
    if before is None:
        pytest.skip("Postgres was not reachable when the session started")
    strict = os.environ.get("CI") == "true" or os.environ.get("ATLAS_TEST_STRICT") == "1"
    after = livedb.snapshot()
    assert after is not None, "Postgres stopped answering during the run"
    background = [] if strict else [t for t in livedb.drift(before, after) if t in livedb.LIVE_WRITERS]
    if background:
        print(f"\nlive tables the running Atlas wrote during the run (not counted): {', '.join(background)}")
    changed = livedb.drift(before, after, ignore=frozenset() if strict else livedb.LIVE_WRITERS)
    assert not changed, (
        f"the suite changed live tables: {', '.join(changed)}. "
        "Shadow them in the test that writes them (tests/livedb.py shadow())."
    )


def test_drift_names_changed_new_and_dropped_tables() -> None:
    before = {"a": "1 x", "b": "2 y", "gone": "0 z"}
    after = {"a": "1 x", "b": "2 w", "new": "1 v"}
    assert livedb.drift(before, after) == ["b", "gone", "new"]
    assert livedb.drift(before, after, ignore=frozenset({"b"})) == ["gone", "new"]
    assert livedb.drift(before, before) == []
