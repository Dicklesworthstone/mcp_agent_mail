from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

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


@pytest.mark.asyncio
async def test_send_message_requires_sender_token_across_sessions(isolated_env):
    """A fresh session cannot impersonate an existing sender without sender_token."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/spoof-send"})
        sender = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/spoof-send", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        sender_token = sender.data["registration_token"]

    async with Client(server) as attacker_client:
        with pytest.raises(ToolError) as exc_info:
            await attacker_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/spoof-send",
                    "sender_name": "GreenCastle",
                    "to": ["GreenCastle"],
                    "subject": "Forged",
                    "body_md": "This should fail",
                },
            )
        assert "sender_token" in str(exc_info.value)

    async with Client(server) as sender_client:
        result = await sender_client.call_tool(
            "send_message",
                {
                    "project_key": "/security/spoof-send",
                    "sender_name": "GreenCastle",
                    "sender_token": sender_token,
                    "to": ["GreenCastle"],
                    "subject": "Legit",
                    "body_md": "This should succeed",
                },
        )
    assert result.data["verified_sender"] is True
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_search_and_summarize_thread_respect_recipient_visibility(isolated_env):
    """Only senders/recipients, including BCC, can discover a private thread."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/private-thread"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        purple = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "PurpleBear"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]
        purple_token = purple.data["registration_token"]

    async with Client(server) as sender_client:
        await sender_client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "/security/private-thread",
                "requester": "GreenCastle",
                "target": "BlueLake",
                "auto_accept": True,
                "requester_registration_token": green_token,
                "target_registration_token": blue_token,
            },
        )
        await sender_client.call_tool(
            "send_message",
            {
                "project_key": "/security/private-thread",
                "sender_name": "GreenCastle",
                "sender_token": green_token,
                "to": ["GreenCastle"],
                "bcc": ["BlueLake"],
                "subject": "Private plan",
                "body_md": "ultra-secret launch sequence",
                "thread_id": "SEC-THREAD-1",
            },
        )

    async with Client(server) as bcc_client:
        search_result = await bcc_client.call_tool(
            "search_messages",
            {
                "project_key": "/security/private-thread",
                "query": "ultra-secret",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
            },
        )
        assert len(search_result.structured_content["result"]) == 1

        summary_result = await bcc_client.call_tool(
            "summarize_thread",
            {
                "project_key": "/security/private-thread",
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "BlueLake",
                "registration_token": blue_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 1
        assert len(summary_result.data["examples"]) == 1

    async with Client(server) as outsider_client:
        search_result = await outsider_client.call_tool(
            "search_messages",
            {
                "project_key": "/security/private-thread",
                "query": "ultra-secret",
                "agent_name": "PurpleBear",
                "registration_token": purple_token,
            },
        )
        assert search_result.structured_content["result"] == []

        summary_result = await outsider_client.call_tool(
            "summarize_thread",
            {
                "project_key": "/security/private-thread",
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "PurpleBear",
                "registration_token": purple_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 0
        assert summary_result.data["examples"] == []
