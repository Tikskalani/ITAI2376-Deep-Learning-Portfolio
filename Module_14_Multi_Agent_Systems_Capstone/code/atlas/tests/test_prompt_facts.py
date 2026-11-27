"""Career facts in the prompts follow the resume (data/resume is the source of truth).

On 2026-10-22 Trilok settled it: ten years running business operations
(Example Property Group, since 2016), then AI. Three agent
prompts and the news brief's prompt said "a software engineer with eight
years". No model is called here.
"""

from __future__ import annotations

import re

import pytest

from core import news
from core.config import REPO_ROOT

PROMPTS = sorted((REPO_ROOT / "agents").glob("*/system.md"))
WRONG = re.compile(r"\b(eight|8)\s+years\b|software engineer with", re.IGNORECASE)
CEO_LINE = ("He ran business operations for ten years (Example Property Group, since 2016) "
            "and is now studying AI at HCC while building agentic systems.")


def test_there_are_agent_prompts_to_check():
    assert len(PROMPTS) >= 12


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.parent.name)
def test_no_agent_prompt_gives_eight_years(path):
    found = WRONG.search(path.read_text(encoding="utf-8"))
    assert found is None, f"{path.parent.name}: '{found.group(0)}' contradicts the resume"


def test_the_news_brief_prompt_agrees():
    assert WRONG.search(news._SYSTEM) is None


def test_the_ceo_prompt_states_it_as_decided():
    assert CEO_LINE in (REPO_ROOT / "agents" / "ceo" / "system.md").read_text(encoding="utf-8")
