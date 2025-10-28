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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )

        # Unknown recipient returns structured error
        with pytest.raises(ToolError):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "GreenCastle",
                    "to": ["BlueLake"],
                    "subject": "Hello",
                    "body_md": "testing unknown recipient",
                },
            )

        # Retrieve again via non-raising call — implementation may auto-handshake;
        # accept either structured error or success
        res = await client.call_tool_mcp(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Hello",
                "body_md": "testing unknown recipient",
            },
        )
        if res.isError:
            # structured error path
            text = " ".join(getattr(c, "text", "") for c in res.content)
            assert "BlueLake" in text
        else:
            # success path (auto-handshake)
            text = " ".join(getattr(c, "text", "") for c in res.content)
            assert "deliveries" in text

        # Register and ensure sanitized inputs (hyphen stripped/lowercased) route
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        success = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["blue-lake"],
                "subject": "Hello again",
                "body_md": "now routed",
            },
        )
        deliveries = success.data.get("deliveries") or []
        assert deliveries and deliveries[0].get("payload", {}).get("subject") == "Hello again"
