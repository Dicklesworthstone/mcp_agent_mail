from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

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


@pytest.mark.asyncio
async def test_unknown_recipient_reports_structured_error(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "AlphaTeam"},
        )

        # Unknown recipient should not raise ToolExecutionError but return structured payload
        with pytest.raises(ToolError):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "AlphaTeam",
                    "to": ["BetaTeam"],
                    "subject": "Hello",
                    "body_md": "testing unknown recipient",
                },
            )

        # Retrieve raw error payload for additional validation
        res = await client.call_tool_mcp(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "AlphaTeam",
                "to": ["BetaTeam"],
                "subject": "Hello",
                "body_md": "testing unknown recipient",
            },
        )
        assert res.isError is True
        message_text = " ".join(chunk.text for chunk in res.content if getattr(chunk, "text", None))
        assert "BetaTeam" in message_text
        assert "resource://agents/backend" in message_text

        # Register recipient and ensure sanitized inputs resolve (hyphen stripped)
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BetaTeam"},
        )
        success = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "AlphaTeam",
                "to": ["beta-team"],
                "subject": "Hello again",
                "body_md": "now routed",
            },
        )
        deliveries = success.data.get("deliveries") or []
        assert deliveries and deliveries[0].get("payload", {}).get("subject") == "Hello again"
