from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache


@pytest.mark.asyncio
async def test_whois_and_projects_resources(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake", "task_description": "dir"},
        )

        who = await client.call_tool(
            "whois",
            {"project_key": "Backend", "agent_name": "BlueLake"},
        )
        assert who.data.get("name") == "BlueLake"
        assert who.data.get("program") == "codex"

        # Projects list
        blocks = await client.read_resource("resource://tooling/projects")
        assert blocks and "backend" in (blocks[0].text or "")

        # Project detail
        blocks2 = await client.read_resource("resource://project/backend")
        assert blocks2 and "BlueLake" in (blocks2[0].text or "")


@pytest.mark.asyncio
async def test_identity_resource_supports_encoded_absolute_project_paths(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    clear_settings_cache()

    target = tmp_path / "repo"
    target.mkdir()
    server = build_mcp_server()

    async with Client(server) as client:
        encoded_target = quote(str(target), safe="")
        blocks = await client.read_resource(f"resource://identity/{encoded_target}")

    payload = json.loads(blocks[0].text)
    assert payload["human_key"] == str(target)
    assert payload["canonical_path"] == str(target)
