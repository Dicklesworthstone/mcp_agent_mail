from __future__ import annotations

import datetime as _dt

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session


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

        # Backdate created_ts in DB to ensure it's older than 1 minute
        backdate = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        async with get_session() as session:
            await session.execute(text("UPDATE messages SET created_ts = :ts WHERE id = :mid"), {"ts": backdate, "mid": mid})
            await session.commit()

        # ack-overdue with ttl_minutes=1 should include it
        blocks2 = await client.read_resource("resource://views/ack-overdue/Recv?project=Backend&ttl_minutes=1&limit=10")
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


@pytest.mark.asyncio
async def test_outbox_and_message_resource(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Sender"},
        )
        m = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Sender",
                "to": ["Sender"],
                "subject": "OutboxMsg",
                "body_md": "B",
            },
        )
        payload = (m.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = payload.get("id")

        # Outbox should list it
        blocks = await client.read_resource("resource://outbox/Sender?project=Backend&limit=5")
        assert blocks and "OutboxMsg" in (blocks[0].text or "")

        # Message resource returns full payload with body
        blocks2 = await client.read_resource(f"resource://message/{mid}?project=Backend")
        assert blocks2 and "OutboxMsg" in (blocks2[0].text or "")


