from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_whois_and_projects_resources(isolated_env):
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
        blocks = await client.read_resource("resource://projects")
        assert blocks and "backend" in (blocks[0].text or "")

        # Project detail
        blocks2 = await client.read_resource("resource://project/backend")
        assert blocks2 and "BlueLake" in (blocks2[0].text or "")


