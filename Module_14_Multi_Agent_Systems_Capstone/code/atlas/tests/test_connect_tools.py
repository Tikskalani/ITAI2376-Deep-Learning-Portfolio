"""Settings, Connections lists what each MCP server lets Atlas do, grouped by app.
The server is never called for real: check_mcp is stubbed."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core import connect
from core.web.api import app

ZAPIER = ["get_dynamic_properties_schema", "list_dynamic_enum_values", "get_configuration_url",
          "github_create_issue", "github_find_issue", "gmail_create_draft", "gmail_create_draft_reply",
          "gmail_find_email", "google_calendar_create_detailed_event", "google_calendar_find_events",
          "google_calendar_update_event", "google_contacts_find_contact", "google_docs_create_document_from_text",
          "google_drive_find_a_file"]


def test_group_tools_by_app():
    groups = connect.group_tools(ZAPIER)
    by_app = {g["app"]: [t["action"] for t in g["tools"]] for g in groups}
    assert by_app["Gmail"] == ["Create draft", "Create draft reply", "Find email"]
    assert by_app["Google Calendar"] == ["Create detailed event", "Find events", "Update event"]
    assert by_app["Google Drive"] == ["Find a file"]
    assert by_app["GitHub"] == ["Create issue", "Find issue"]
    assert set(by_app["Zapier helpers"]) == {"Get dynamic properties schema", "List dynamic enum values", "Get configuration url"}
    # Apps first by name, the helpers last.
    assert [g["app"] for g in groups][-1] == "Zapier helpers"
    assert sum(len(g["tools"]) for g in groups) == len(ZAPIER)


def test_group_tools_unknown_prefix_falls_back_to_first_word():
    groups = connect.group_tools(["acme_widgets_list", "acme_widgets_make", "lonely"])
    by_app = {g["app"]: [t["action"] for t in g["tools"]] for g in groups}
    assert by_app["Acme"] == ["Widgets list", "Widgets make"]
    assert by_app["Other"] == ["Lonely"]


@pytest.fixture
def one_server(monkeypatch):
    entries = [{"name": "zapier", "url": "https://mcp.zapier.com/api/mcp/s/secret/mcp", "token": "t0k", "agents": ["ceo"]}]
    monkeypatch.setattr(connect.envfile, "get", lambda key, path=None: json.dumps(entries) if key == "ATLAS_MCP_SERVERS" else None)
    calls: list[tuple[str, Any]] = []

    async def fake_check(url: str, token: str | None = None) -> dict[str, Any]:
        calls.append((url, token))
        return {"ok": True, "server": "zapier", "tools": ZAPIER, "count": len(ZAPIER)}

    monkeypatch.setattr(connect, "check_mcp", fake_check)
    connect._TOOLS_CACHE.clear()
    return calls


async def test_tools_route_groups_and_caches(one_server):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/connect/mcp/zapier/tools")
        r2 = await c.get("/api/connect/mcp/zapier/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "zapier" and body["count"] == len(ZAPIER)
    assert any(g["app"] == "Gmail" for g in body["groups"])
    assert "secret" not in r.text and "t0k" not in r.text, "the URL and the token never leave the laptop"
    assert len(one_server) == 1, "a second look within ten minutes reuses the answer"
    assert r2.json()["groups"] == body["groups"]


async def test_tools_route_unknown_server(one_server):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atlas") as c:
        r = await c.get("/api/connect/mcp/notion/tools")
    assert r.status_code == 404
