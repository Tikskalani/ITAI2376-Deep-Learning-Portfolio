"""The morning AI brief (core/news.py): feeds in, a handful of stories out.

The parser is tested on RSS and Atom documents. The sweep, the pick and
the brief run against private copies of news_items, briefs and
notifications, with the network and the model replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from core import db, news
from core.web.api import app
from tests import livedb

aio = pytest.mark.asyncio(loop_scope="module")
NOW = datetime(2026, 10, 11, 10, 45, tzinfo=UTC)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>OpenAI</title>
<item><title>Introducing the Agents SDK 2.0</title><link>https://openai.com/agents-2</link>
  <guid>openai-agents-2</guid><pubDate>Sat, 10 Oct 2026 16:00:00 GMT</pubDate>
  <description><![CDATA[<p>A new <b>runtime</b> for agents &amp; tools.</p>]]></description></item>
<item><title>Old news</title><link>https://openai.com/old</link><pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title></title><link>https://openai.com/blank</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Simon Willison</title>
<entry><title>MCP 2026-07-28 in practice</title>
  <link rel="alternate" href="https://simonwillison.net/mcp-2026"/><link rel="self" href="https://x/self"/>
  <id>tag:simonwillison.net,2026:mcp</id><published>2026-10-11T08:30:00+00:00</published>
  <summary type="html">&lt;p&gt;What the stateless core changes for &lt;em&gt;servers&lt;/em&gt;.&lt;/p&gt;</summary></entry>
<entry><title>Undated note</title><link href="https://simonwillison.net/undated"/><id>u1</id>
  <content>Just a note.</content></entry>
</feed>"""


# ------------------------------------------------------------------ the parser


def test_parse_rss_and_atom():
    rss = news.parse_feed(RSS, "OpenAI")
    assert [i.title for i in rss] == ["Introducing the Agents SDK 2.0", "Old news"]
    a = rss[0]
    assert a.url == "https://openai.com/agents-2" and a.summary == "A new runtime for agents & tools."
    assert a.published_at == datetime(2026, 10, 10, 16, tzinfo=UTC) and a.external_id == news._key("openai-agents-2")
    assert rss[1].external_id == news._key("https://openai.com/old")

    atom = news.parse_feed(ATOM, "Simon Willison")
    assert [i.title for i in atom] == ["MCP 2026-07-28 in practice", "Undated note"]
    b = atom[0]
    assert b.url == "https://simonwillison.net/mcp-2026" and b.summary == "What the stateless core changes for servers."
    assert b.published_at == datetime(2026, 10, 11, 8, 30, tzinfo=UTC)
    assert atom[1].published_at is None and atom[1].summary == "Just a note." and atom[1].url == "https://simonwillison.net/undated"

    assert news.parse_feed("<html>not a feed</html>", "x") == []
    assert news.parse_feed("<<<", "x") == []
    bomb = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xxxx">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    assert news.parse_feed(bomb, "x") == []          # entities are refused, not expanded


def test_the_config_names_real_feeds():
    cfg = news.load_config()
    assert len(cfg["feeds"]) >= 8 and all(f["url"].startswith("https://") and f["name"] for f in cfg["feeds"])
    assert cfg["window_hours"] == 36 and cfg["picks"] == 6


def test_render_is_prose_with_the_link_at_the_end():
    text = news.render([{"headline": "OpenAI ships an agents runtime", "why": "You can compare it to Atlas's loop",
                         "source": "OpenAI", "url": "https://openai.com/agents-2"}], datetime(2026, 10, 11, 7))
    assert text.startswith("AI this morning, Sunday, October 11. 1 story worth your time.")
    assert "OpenAI ships an agents runtime. You can compare it to Atlas's loop. (OpenAI) https://openai.com/agents-2" in text


# ------------------------------------------------------------------ the sweep and the brief


@pytest_asyncio.fixture(loop_scope="module")
async def world(monkeypatch):
    async with livedb.shadow(monkeypatch, "news_items", "briefs", "notifications", "events", "llm_calls"):
        yield


def _fetch(*docs):
    async def fetch(feeds):
        return [it for text, src in docs for it in news.parse_feed(text, src)]
    return fetch


def _choose(picks):
    calls: list[dict] = []

    async def choose(**kw):
        calls.append(kw)
        return {"picks": picks}
    choose.calls = calls  # type: ignore[attr-defined]
    return choose


CFG = {"feeds": [{"name": "OpenAI", "url": "https://x/rss"}, {"name": "Simon Willison", "url": "https://x/atom"}],
       "window_hours": 36, "picks": 6}


@aio
async def test_scan_files_new_stories_once_and_ignores_the_archive(world):
    out = await news.scan(fetch=_fetch((RSS, "OpenAI"), (ATOM, "Simon Willison")), now=NOW, config=CFG)
    assert out == {"feeds": 2, "stories": 4, "new": 3}            # "Old news" is outside the window
    assert await news.scan(fetch=_fetch((RSS, "OpenAI"), (ATOM, "Simon Willison")), now=NOW, config=CFG) == {"feeds": 2, "stories": 4, "new": 0}
    titles = {r["title"] for r in await db.fetch("select title from news_items")}
    assert titles == {"Introducing the Agents SDK 2.0", "MCP 2026-07-28 in practice", "Undated note"}


@aio
async def test_pick_keeps_valid_numbers_once_and_caps_the_count(world):
    items = [{"id": UUID(int=n), "source": "s", "title": f"t{n}", "summary": "", "published_at": NOW} for n in range(1, 5)]
    chooser = _choose([{"n": 2, "headline": "Two", "why": "because"}, {"n": 2, "headline": "dup", "why": ""},
                       {"n": 9, "headline": "out of range", "why": ""}, {"n": "x", "headline": "bad", "why": ""},
                       {"n": 4, "headline": "  Four  ", "why": "  w  "}, {"n": 1, "headline": "One", "why": "w"}])
    picks = await news.pick(items, limit=2, choose=chooser)
    assert [(p["title"], p["headline"], p["why"]) for p in picks] == [("t2", "Two", "because"), ("t4", "Four", "w")]
    assert "Stories since yesterday" in chooser.calls[0]["user"] and "2. [s, Oct 11] t2" in chooser.calls[0]["user"]
    assert "between 3 and 2 stories" in chooser.calls[0]["system"]
    assert await news.pick([], choose=chooser) == []

    async def broken(**kw):
        raise RuntimeError("api down")
    assert await news.pick(items, choose=broken) == []


@aio
async def test_the_morning_brief_end_to_end(world):
    chooser = _choose([{"n": 1, "headline": "MCP's stateless core, explained", "why": "Atlas mounts streamable HTTP; check the SDK pin."},
                       {"n": 2, "headline": "OpenAI ships Agents SDK 2.0", "why": "Compare its runtime to Atlas's loop before an interview."}])
    out = await news.run_morning_brief(fetch=_fetch((RSS, "OpenAI"), (ATOM, "Simon Willison")), choose=chooser, now=NOW, config=CFG)
    assert out["new"] == 3 and out["considered"] == 3 and out["picked"] == 2 and out["brief_id"]
    brief = await db.fetchrow("select kind, body, notification_id from briefs where id = $1", out["brief_id"])
    assert brief["kind"] == "ai_news" and brief["body"].startswith("AI this morning, Sunday, October 11. 2 stories worth your time.")
    assert "MCP's stateless core, explained. Atlas mounts streamable HTTP; check the SDK pin. (Simon Willison) https://simonwillison.net/mcp-2026" in brief["body"]
    note = await db.fetchrow("select kind, title, data from notifications where id = $1", brief["notification_id"])
    assert note["kind"] == "brief/ai_news" and note["title"] == "AI this morning, Oct 11" and note["data"]["brief_id"] == str(out["brief_id"])
    rows = {r["title"]: r for r in await db.fetch("select title, why, picked_at, brief_id from news_items")}
    assert rows["MCP's stateless core, explained"]["picked_at"] and rows["OpenAI ships Agents SDK 2.0"]["why"].startswith("Compare")
    assert rows["Undated note"]["picked_at"] is None and rows["Undated note"]["brief_id"] == out["brief_id"]   # considered, passed over

    # The Learn tab's view of it.
    seen = await news.listing()
    assert [p["title"] for p in seen["picks"]] == ["MCP's stateless core, explained", "OpenAI ships Agents SDK 2.0"]
    assert seen["latest"]["id"] == out["brief_id"] and seen["latest"]["considered"] == 3

    # Later the same morning: nothing new, nothing to say, no second brief.
    again = await news.run_morning_brief(fetch=_fetch((RSS, "OpenAI"), (ATOM, "Simon Willison")), choose=chooser,
                                         now=NOW + timedelta(hours=1), config=CFG)
    assert again["new"] == 0 and again["considered"] == 0 and again["picked"] == 0 and again["brief_id"] is None
    assert await db.fetchval("select count(*) from briefs") == 1 and len(chooser.calls) == 1


@aio
async def test_routes(world, monkeypatch):
    monkeypatch.setattr(news, "fetch_all", _fetch((ATOM, "Simon Willison")))
    monkeypatch.setattr(news.llm, "complete_json", _choose([{"n": 1, "headline": "MCP in practice", "why": "w"}]))
    monkeypatch.setattr(news, "load_config", lambda path=None: CFG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/news")
        assert r.status_code == 200 and r.json() == {"picks": [], "latest": None}
        r = await c.post("/api/news/run")
        assert r.status_code == 200 and r.json()["picked"] == 1 and r.json()["brief_id"]
        r = await c.get("/api/news?limit=5")
        assert [p["title"] for p in r.json()["picks"]] == ["MCP in practice"] and r.json()["latest"]["considered"] >= 1
