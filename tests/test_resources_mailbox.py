from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_views_ack_required_and_ack_overdue_resources(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Sender"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Recv"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Sender",
                "to": ["Recv"],
                "subject": "NeedsAck",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        # ack-required view should include it
        blocks = await client.read_resource("resource://views/ack-required/Recv?project=Backend&limit=10")
        assert blocks and "NeedsAck" in (blocks[0].text or "")

        # ack-overdue with ttl_minutes=0 should include it as overdue
        blocks2 = await client.read_resource("resource://views/ack-overdue/Recv?project=Backend&ttl_minutes=0&limit=10")
        assert blocks2 and "NeedsAck" in (blocks2[0].text or "")

        # After acknowledgement, it should disappear from ack-required
        await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "Recv", "message_id": mid},
        )
        blocks3 = await client.read_resource("resource://views/ack-required/Recv?project=Backend&limit=10")
        # Either empty or not containing the subject
        content = "\n".join(b.text or "" for b in blocks3)
        assert "NeedsAck" not in content


@pytest.mark.asyncio
async def test_mailbox_and_mailbox_with_commits(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "User"},
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "User",
                "to": ["User"],
                "subject": "CommitMeta",
                "body_md": "body",
            },
        )

        # Basic mailbox
        blocks = await client.read_resource("resource://mailbox/User?project=Backend&limit=5")
        assert blocks and "CommitMeta" in (blocks[0].text or "")

        # With commits metadata
        blocks2 = await client.read_resource("resource://mailbox-with-commits/User?project=Backend&limit=5")
        assert blocks2 and "CommitMeta" in (blocks2[0].text or "")


