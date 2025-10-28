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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "RedStone"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["RedStone"],
                "subject": "NeedsAck",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        # ack-required view should include it
        blocks = await client.read_resource("resource://views/ack-required/RedStone?project=Backend&limit=10")
        assert blocks and "NeedsAck" in (blocks[0].text or "")

        # Backdate created_ts in DB to ensure it's older than 1 minute
        backdate = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        async with get_session() as session:
            await session.execute(text("UPDATE messages SET created_ts = :ts WHERE id = :mid"), {"ts": backdate, "mid": mid})
            await session.commit()

        # ack-overdue with ttl_minutes=1 should include it
        blocks2 = await client.read_resource("resource://views/ack-overdue/RedStone?project=Backend&ttl_minutes=1&limit=10")
        assert blocks2 and "NeedsAck" in (blocks2[0].text or "")

        # After acknowledgement, it should disappear from ack-required
        await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        blocks3 = await client.read_resource("resource://views/ack-required/RedStone?project=Backend&limit=10")
        # Either empty or not containing the subject
        content = "\n".join(b.text or "" for b in blocks3)
        assert "NeedsAck" not in content


@pytest.mark.asyncio
async def test_mailbox_and_mailbox_with_commits(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "CommitMeta",
                "body_md": "body",
            },
        )

        # Basic mailbox
        blocks = await client.read_resource("resource://mailbox/BlueLake?project=Backend&limit=5")
        assert blocks and "CommitMeta" in (blocks[0].text or "")

        # With commits metadata
        blocks2 = await client.read_resource("resource://mailbox-with-commits/BlueLake?project=Backend&limit=5")
        assert blocks2 and "CommitMeta" in (blocks2[0].text or "")


@pytest.mark.asyncio
async def test_outbox_and_message_resource(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        m = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
                "subject": "OutboxMsg",
                "body_md": "B",
            },
        )
        payload = (m.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = payload.get("id")

        # Outbox should list it
        blocks = await client.read_resource("resource://outbox/GreenCastle?project=Backend&limit=5")
        assert blocks and "OutboxMsg" in (blocks[0].text or "")

        # Message resource returns full payload with body
        blocks2 = await client.read_resource(f"resource://message/{mid}?project=Backend")
        assert blocks2 and "OutboxMsg" in (blocks2[0].text or "")


