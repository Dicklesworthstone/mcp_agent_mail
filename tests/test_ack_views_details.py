from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_ack_overdue_and_stale_detail_fields(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "send_message",
                {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "A1", "body_md": "x", "ack_required": True},
        )
        # acks-stale with small ttl should include age_seconds field when stale
        stale = await client.read_resource("resource://views/acks-stale/BlueLake?project=Backend&ttl_seconds=0&limit=5")
        assert stale and "age_seconds" in (stale[0].text or "")
        # ack-overdue with ttl_minutes 0 should list messages
        overdue = await client.read_resource("resource://views/ack-overdue/BlueLake?project=Backend&ttl_minutes=0&limit=5")
        assert overdue and "messages" in (overdue[0].text or "")

