from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_invalid_project_or_agent_errors(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        # Missing project — use non-raising MCP call to inspect error payload
        res = await client.call_tool_mcp("register_agent", {"project_key": "Missing", "program": "x", "model": "y", "name": "A"})
        assert res.isError is True
        # Now create project and try sending from unknown agent
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        res2 = await client.call_tool_mcp(
            "send_message",
            {"project_key": "Backend", "sender_name": "Ghost", "to": ["Ghost"], "subject": "x", "body_md": "y"},
        )
        # Should be error due to unknown agent
        assert res2.isError is True


