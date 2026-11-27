"""One agent's record for the Staff room (/api/agents/{name}, audit finding 09).

Shadowed episodes only; no model call."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from core import db
from core.web.api import app
from tests import livedb

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(autouse=True)
def _roster():
    from core.runtime.register_all import register_all

    register_all()


@pytest_asyncio.fixture(loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        yield c


async def test_an_agent_page_shows_purpose_runs_spend_and_traces(client, monkeypatch):
    async with livedb.shadow(monkeypatch, "episodes", "llm_calls"):
        for status, ago in (("ok", "1 hour"), ("failed", "2 days"), ("ok", "20 days")):
            await db.execute(
                "insert into episodes (agent_name, summary, started_at, status) "
                f"values ('cfo', 'filed a statement', now() - interval '{ago}', $1)", status)
        await db.execute("insert into episodes (agent_name, summary, started_at) values ('coo', 'other', now())")
        r = await client.get("/api/agents/cfo")
        assert r.status_code == 200
        page = r.json()
        assert page["registered"] and page["purpose"].startswith("You are the CFO")
        assert (page["runs_7d"], page["runs_30d"], page["failed_7d"]) == (2, 3, 1)
        assert len(page["episodes"]) == 3 and page["episodes"][0]["status"] == "ok"
        assert all(e["id"] for e in page["episodes"]) and page["month_spent"] is not None
        assert page["tools"], "the CFO's tools are listed"


async def test_unknown_and_malformed_names_are_404(client, monkeypatch):
    async with livedb.shadow(monkeypatch, "episodes"):
        assert (await client.get("/api/agents/nobody_here")).status_code == 404
        assert (await client.get("/api/agents/..%2Fsecrets")).status_code == 404
        assert (await client.get("/api/agents/CFO")).status_code == 404
