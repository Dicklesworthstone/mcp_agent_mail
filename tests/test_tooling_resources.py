from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_tooling_directory_and_metrics_populate(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "Alpha", "to": ["Alpha"], "subject": "Ping", "body_md": "x"},
        )
        # Directory
        blocks = await client.read_resource("resource://tooling/directory")
        assert blocks
        body = blocks[0].text or ""
        assert "messaging" in body or "claims" in body
        # Metrics
        blocks2 = await client.read_resource("resource://tooling/metrics")
        assert blocks2 and "tools" in (blocks2[0].text or "")


@pytest.mark.asyncio
async def test_tooling_recent_filters(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "health_check",
            {},
        )
        blocks = await client.read_resource("resource://tooling/recent/60?agent=Alpha&project=Backend")
        # Expect entries slice mentioning tools
        assert blocks and ("tools" in (blocks[0].text or "") or "entries" in (blocks[0].text or ""))


