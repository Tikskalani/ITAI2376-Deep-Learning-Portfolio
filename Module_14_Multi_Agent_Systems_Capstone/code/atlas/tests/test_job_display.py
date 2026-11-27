"""How the Jobs tab names companies and groups roles (audit 2026-10-22, finding 15).

Pure functions: no database, no model."""

from __future__ import annotations

import pytest

from core.jobs.mail import _clean_company, shown_company
from core.web.jobs import one_per_role


@pytest.mark.parametrize("name", ["Indeed employer", "an Indeed Employer", "LinkedIn employer", "Employer",
                                  "the hiring company"])
def test_a_board_placeholder_is_not_a_company(name):
    assert _clean_company(name) is None


def test_real_companies_pass_and_the_placeholder_reads_as_what_it_is():
    assert _clean_company("Employer Direct Inc") == "Employer Direct Inc"
    assert shown_company("Bain & Company") == "Bain & Company"
    assert shown_company("Indeed employer") == "Company not named (via Indeed)"
    assert shown_company("") == "Company not named"


def test_one_card_per_role_with_every_location():
    rows = [
        {"id": 1, "company": "OpenAI", "title": "Applied AI Engineer, API Platform", "location": "San Francisco", "score": 70},
        {"id": 2, "company": "Cursor", "title": "Forward Deployed Engineer", "location": "New York", "score": 68},
        {"id": 3, "company": "openai", "title": "Applied AI Engineer,  API Platform", "location": "New York City", "score": 65},
        {"id": 4, "company": "OpenAI", "title": "Applied AI Engineer, API Platform", "location": "San Francisco", "score": 60},
    ]
    out = one_per_role(rows)
    assert [r["id"] for r in out] == [1, 2]
    assert out[0]["locations"] == ["San Francisco", "New York City"]
    assert out[1]["locations"] == ["New York"]
    assert out[0]["ids"] == ["1", "3", "4"]


def test_unnamed_employers_are_never_merged():
    rows = [{"id": 1, "company": "Indeed employer", "title": "AI Engineer", "location": "Austin"},
            {"id": 2, "company": "Indeed employer", "title": "AI Engineer", "location": "Dallas"}]
    assert len(one_per_role(rows)) == 2


def test_every_agent_speaks_to_him_as_you():
    from core.runtime.agent import VOICE_RULE, load_agent_config

    cfg = load_agent_config("career", "sonnet")
    assert cfg.system_prompt.endswith(VOICE_RULE) and '"you"' in VOICE_RULE
