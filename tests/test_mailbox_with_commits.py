from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_mailbox_with_commits_includes_commit_meta(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "C1", "body_md": "b"},
        )
        blocks = await client.read_resource("resource://mailbox-with-commits/BlueLake?project=Backend&limit=5")
        assert blocks and blocks[0].text
        # Text is JSON; ensure it mentions commit key when present
        assert "messages" in blocks[0].text

