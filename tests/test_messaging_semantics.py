from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_reply_message_inherits_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        orig_id = int(msg.get("id"))
        # Reply
        r = await client.call_tool(
            "reply_message",
            {"project_key": "Backend", "message_id": orig_id, "sender_name": "BlueLake", "body_md": "ack"},
        )
        rdata = r.data
        expected_thread = msg.get("thread_id") or str(orig_id)
        assert rdata.get("thread_id") == expected_thread
        assert str(rdata.get("reply_to")) == str(orig_id)
        # Subject on delivery payload should be prefixed
        deliveries = rdata.get("deliveries") or []
        assert deliveries
        subj = deliveries[0].get("payload", {}).get("subject", "")
        assert subj.lower().startswith("re:")


@pytest.mark.asyncio
async def test_mark_read_then_ack_updates_state(isolated_env):
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
                "subject": "AckPlease",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        mr = await client.call_tool(
            "mark_message_read",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        assert mr.data.get("read") is True and isinstance(mr.data.get("read_at"), str)

        ack = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        assert ack.data.get("acknowledged") is True
        assert isinstance(ack.data.get("acknowledged_at"), str)
        assert isinstance(ack.data.get("read_at"), str)


@pytest.mark.asyncio
async def test_acknowledge_idempotent_multiple_calls(isolated_env):
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
                "subject": "AckTwice",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        first = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        first_ack_at = first.data.get("acknowledged_at")
        assert first.data.get("acknowledged") is True and isinstance(first_ack_at, str)

        second = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        # Timestamps should remain the same (idempotent)
        assert second.data.get("acknowledged_at") == first_ack_at


