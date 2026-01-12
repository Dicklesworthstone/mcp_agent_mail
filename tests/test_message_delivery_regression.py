"""P1 Regression Tests: Core Message Delivery Flow.

These tests verify the core message delivery functionality including:
1. Basic message sending (send_message)
2. Multiple recipients (to, cc, bcc)
3. Thread creation and continuation
4. Importance levels and ack_required flags
5. Reply message functionality
6. Inbox/outbox synchronization
7. Message read and acknowledgment tracking

Reference: mcp_agent_mail-uvf
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

from mcp_agent_mail.app import _get_agent, _get_project_by_identifier, _list_outbox, build_mcp_server
from mcp_agent_mail.db import ensure_schema, track_queries


def get_field(obj: Any, field: str) -> Any:
    """Get a field from either a dict or an object with attributes."""
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def get_inbox_items(result) -> list[dict]:
    """Extract inbox items from a call_tool result as a list of dicts.

    FastMCP may return items in different formats, this handles them all.
    """
    # Try structured_content first (most reliable)
    if hasattr(result, "structured_content") and result.structured_content:
        sc = result.structured_content
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        if isinstance(sc, list):
            return sc

    # Fall back to result.data if it's a proper list of dicts
    if hasattr(result, "data") and isinstance(result.data, list):
        items = []
        for item in result.data:
            if isinstance(item, dict):
                items.append(item)
            elif hasattr(item, "model_dump"):
                items.append(item.model_dump())
            elif hasattr(item, "__dict__") and item.__dict__:
                items.append(item.__dict__)
            else:
                # Empty Root objects - skip
                continue
        if items:
            return items

    return []


# ============================================================================
# Helper fixtures
# ============================================================================


async def setup_project_with_agents(client: Client, project_key: str, count: int = 2):
    """Helper to set up a project with multiple agents (auto-generated names).

    Returns a list of agent names in the order they were created.
    """
    await client.call_tool("ensure_project", {"human_key": project_key})
    agent_names = []
    for _ in range(count):
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test",
                "model": "test",
                # Don't specify name - let system auto-generate valid adjective+noun name
            },
        )
        name = result.data["name"]
        agent_names.append(name)
        # Set open contact policy for testing
        await client.call_tool(
            "set_contact_policy",
            {"project_key": project_key, "agent_name": name, "policy": "open"},
        )
    return agent_names


# ============================================================================
# Basic Message Sending Tests
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_returns_delivery_info(isolated_env):
    """send_message should return delivery count and deliveries list."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/send", count=2)
        sender, receiver = agents[0], agents[1]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/send",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Test Message",
                "body_md": "This is a test message body.",
            },
        )

        assert result.data["count"] == 1
        assert len(result.data["deliveries"]) == 1
        delivery = result.data["deliveries"][0]
        assert "payload" in delivery
        assert delivery["payload"]["subject"] == "Test Message"
        assert delivery["payload"]["from"] == sender


@pytest.mark.asyncio
async def test_send_message_self_send(isolated_env):
    """An agent should be able to send messages to itself."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/self", count=1)
        agent = agents[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/self",
                "sender_name": agent,
                "to": [agent],
                "subject": "Note to Self",
                "body_md": "Remember this.",
            },
        )

        assert result.data["count"] == 1
        # Message should appear in sender's inbox
        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/self",
                "agent_name": agent,
                "limit": 10,
            },
        )
        items = get_inbox_items(inbox)
        assert len(items) >= 1
        assert any(m.get("subject") == "Note to Self" for m in items)


@pytest.mark.asyncio
async def test_send_message_message_id_returned(isolated_env):
    """send_message should return the message ID in the delivery payload."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/id", count=2)
        sender, receiver = agents[0], agents[1]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/id",
                "sender_name": sender,
                "to": [receiver],
                "subject": "ID Test",
                "body_md": "Testing message ID.",
            },
        )

        payload = result.data["deliveries"][0]["payload"]
        assert "id" in payload
        assert isinstance(payload["id"], int)
        assert payload["id"] > 0


# ============================================================================
# Multiple Recipients Tests
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_multiple_to_recipients(isolated_env):
    """send_message should deliver to multiple recipients in 'to' field."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/multi", count=4)
        sender = agents[0]
        receivers = agents[1:4]  # 3 receivers

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/multi",
                "sender_name": sender,
                "to": receivers,
                "subject": "Broadcast",
                "body_md": "Message to all.",
            },
        )

        # Should show delivery count of 1 (one message to multiple recipients)
        assert result.data["count"] == 1
        # But delivery should list all recipients
        delivery = result.data["deliveries"][0]
        assert len(delivery["payload"]["to"]) == 3

        # Each recipient should have the message in their inbox
        for recv in receivers:
            inbox = await client.call_tool(
                "fetch_inbox",
                {"project_key": "/test/multi", "agent_name": recv, "limit": 10},
            )
            items = get_inbox_items(inbox)
            assert any(m.get("subject") == "Broadcast" for m in items)


@pytest.mark.asyncio
async def test_send_message_recipient_lookup_query_count(isolated_env):
    """Sending to many recipients should not scale agent lookup queries linearly."""
    server = build_mcp_server()
    project_key = "/test/query-count"
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})

        sender_payload = await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "test", "model": "test"},
        )
        sender = sender_payload.data["name"]

        recipients: list[str] = []
        for _ in range(3):
            payload = await client.call_tool(
                "register_agent",
                {"project_key": project_key, "program": "test", "model": "test"},
            )
            recipients.append(payload.data["name"])

        for name in [sender, *recipients]:
            await client.call_tool(
                "set_contact_policy",
                {"project_key": project_key, "agent_name": name, "policy": "open"},
            )

        with track_queries() as tracker:
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": sender,
                    "to": recipients,
                    "subject": "Query Count",
                    "body_md": "Benchmarking query count.",
                    "ack_required": True,
                },
            )

    agents_queries = tracker.per_table.get("agents", 0)
    assert agents_queries <= 2, f"Expected <= 2 agent queries, got {agents_queries}"


@pytest.mark.asyncio
async def test_list_outbox_recipient_lookup_query_count(isolated_env):
    """Listing outbox recipients should not fetch recipients per message."""
    server = build_mcp_server()
    project_key = "/test/outbox-query"
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})

        sender_payload = await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "test", "model": "test"},
        )
        sender = sender_payload.data["name"]

        recipients: list[str] = []
        for _ in range(3):
            payload = await client.call_tool(
                "register_agent",
                {"project_key": project_key, "program": "test", "model": "test"},
            )
            recipients.append(payload.data["name"])

        for name in [sender, *recipients]:
            await client.call_tool(
                "set_contact_policy",
                {"project_key": project_key, "agent_name": name, "policy": "open"},
            )

        for idx in range(2):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": sender,
                    "to": recipients,
                    "subject": f"Outbox Query {idx}",
                    "body_md": "Outbox query benchmark.",
                },
            )

        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, sender)
        await ensure_schema()
        with track_queries() as tracker:
            items = await _list_outbox(project, agent, limit=10, include_bodies=False, since_ts=None)

    assert len(items) >= 2
    messages_queries = tracker.per_table.get("messages", 0)
    recipients_queries = tracker.per_table.get("message_recipients", 0)
    assert messages_queries <= 1, f"Expected <= 1 messages query, got {messages_queries}"
    assert recipients_queries <= 1, f"Expected <= 1 recipient query, got {recipients_queries}"


@pytest.mark.asyncio
async def test_send_message_with_cc_recipients(isolated_env):
    """send_message should deliver to CC recipients."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/cc", count=4)
        sender = agents[0]
        to_recv = agents[1]
        cc_recvs = agents[2:4]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/cc",
                "sender_name": sender,
                "to": [to_recv],
                "cc": cc_recvs,
                "subject": "CC Test",
                "body_md": "Message with CC.",
            },
        )

        # 1 message delivered to 3 recipients (1 to + 2 cc)
        assert result.data["count"] == 1
        delivery = result.data["deliveries"][0]
        assert len(delivery["payload"]["to"]) == 1
        assert len(delivery["payload"]["cc"]) == 2

        # All should receive the message
        for recv in [to_recv, *cc_recvs]:
            inbox = await client.call_tool(
                "fetch_inbox",
                {"project_key": "/test/cc", "agent_name": recv, "limit": 10},
            )
            items = get_inbox_items(inbox)
            assert any(m.get("subject") == "CC Test" for m in items)


@pytest.mark.asyncio
async def test_send_message_with_bcc_recipients(isolated_env):
    """send_message should deliver to BCC recipients without revealing them."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/bcc", count=3)
        sender = agents[0]
        to_recv = agents[1]
        bcc_recv = agents[2]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcc",
                "sender_name": sender,
                "to": [to_recv],
                "bcc": [bcc_recv],
                "subject": "BCC Test",
                "body_md": "Message with BCC.",
            },
        )

        # 1 message delivered to 2 recipients (1 to + 1 bcc)
        assert result.data["count"] == 1
        delivery = result.data["deliveries"][0]
        assert len(delivery["payload"]["to"]) == 1
        assert len(delivery["payload"]["bcc"]) == 1

        # BCC recipient should receive the message
        bcc_inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test/bcc", "agent_name": bcc_recv, "limit": 10},
        )
        items = get_inbox_items(bcc_inbox)
        assert any(m.get("subject") == "BCC Test" for m in items)


# ============================================================================
# Thread Tests
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_creates_thread_id(isolated_env):
    """First message with thread_id should create the thread."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/thread", count=2)
        sender, receiver = agents[0], agents[1]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/thread",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Thread Start",
                "body_md": "Starting a thread.",
                "thread_id": "THREAD-001",
            },
        )

        payload = result.data["deliveries"][0]["payload"]
        assert payload["thread_id"] == "THREAD-001"


@pytest.mark.asyncio
async def test_send_message_continues_thread(isolated_env):
    """Subsequent messages with same thread_id should continue the thread."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/thread2", count=2)
        agent_a, agent_b = agents[0], agents[1]

        # First message
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/thread2",
                "sender_name": agent_a,
                "to": [agent_b],
                "subject": "Thread Message 1",
                "body_md": "First message.",
                "thread_id": "THREAD-002",
            },
        )

        # Second message in same thread
        result2 = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/thread2",
                "sender_name": agent_b,
                "to": [agent_a],
                "subject": "Thread Message 2",
                "body_md": "Second message.",
                "thread_id": "THREAD-002",
            },
        )

        payload2 = result2.data["deliveries"][0]["payload"]
        assert payload2["thread_id"] == "THREAD-002"


# ============================================================================
# Importance and Ack Required Tests
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_importance_levels(isolated_env):
    """send_message should respect importance parameter."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/importance", count=2)
        sender, receiver = agents[0], agents[1]

        for level in ["low", "normal", "high", "urgent"]:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/importance",
                    "sender_name": sender,
                    "to": [receiver],
                    "subject": f"Importance: {level}",
                    "body_md": f"Message with {level} importance.",
                    "importance": level,
                },
            )
            payload = result.data["deliveries"][0]["payload"]
            assert payload["importance"] == level


@pytest.mark.asyncio
async def test_send_message_ack_required_flag(isolated_env):
    """send_message should set ack_required flag correctly."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/ack", count=2)
        sender, receiver = agents[0], agents[1]

        # With ack_required=True
        result_ack = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/ack",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Needs Ack",
                "body_md": "Please acknowledge.",
                "ack_required": True,
            },
        )
        assert result_ack.data["deliveries"][0]["payload"]["ack_required"] is True

        # With ack_required=False (default)
        result_no_ack = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/ack",
                "sender_name": sender,
                "to": [receiver],
                "subject": "No Ack Needed",
                "body_md": "No acknowledgment needed.",
                "ack_required": False,
            },
        )
        assert result_no_ack.data["deliveries"][0]["payload"]["ack_required"] is False


# ============================================================================
# Inbox Tests
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_inbox_returns_messages(isolated_env):
    """fetch_inbox should return messages for the agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/inbox", count=2)
        sender, receiver = agents[0], agents[1]

        # Send multiple messages
        for i in range(3):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/inbox",
                    "sender_name": sender,
                    "to": [receiver],
                    "subject": f"Message {i + 1}",
                    "body_md": f"Body {i + 1}",
                },
            )

        inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test/inbox", "agent_name": receiver, "limit": 10},
        )

        items = get_inbox_items(inbox)
        assert len(items) == 3
        subjects = [m.get("subject") for m in items]
        assert "Message 1" in subjects
        assert "Message 2" in subjects
        assert "Message 3" in subjects


@pytest.mark.asyncio
async def test_fetch_inbox_urgent_only_filter(isolated_env):
    """fetch_inbox with urgent_only should only return high/urgent messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/urgent", count=2)
        sender, receiver = agents[0], agents[1]

        # Send messages with different importance
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/urgent",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Normal Message",
                "body_md": "Normal importance.",
                "importance": "normal",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/urgent",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Urgent Message",
                "body_md": "Urgent importance.",
                "importance": "urgent",
            },
        )

        # Fetch with urgent_only
        urgent_inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/urgent",
                "agent_name": receiver,
                "urgent_only": True,
                "limit": 10,
            },
        )

        items = get_inbox_items(urgent_inbox)
        assert len(items) == 1
        assert items[0].get("subject") == "Urgent Message"


@pytest.mark.asyncio
async def test_fetch_inbox_include_bodies(isolated_env):
    """fetch_inbox with include_bodies=True should include message bodies."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/bodies", count=2)
        sender, receiver = agents[0], agents[1]

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bodies",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Has Body",
                "body_md": "This is the full message body content.",
            },
        )

        # Without bodies
        await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/bodies",
                "agent_name": receiver,
                "include_bodies": False,
                "limit": 10,
            },
        )
        # Body may or may not be included when False (implementation dependent)

        # With bodies
        inbox_with_body = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/bodies",
                "agent_name": receiver,
                "include_bodies": True,
                "limit": 10,
            },
        )
        items = get_inbox_items(inbox_with_body)
        assert len(items) >= 1
        msg_dict = items[0]
        assert "body_md" in msg_dict
        assert "full message body" in msg_dict["body_md"]


# ============================================================================
# Reply Message Tests
# ============================================================================


@pytest.mark.asyncio
async def test_reply_message_creates_thread_link(isolated_env):
    """reply_message should link to the original message and thread."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/reply", count=2)
        agent_a, agent_b = agents[0], agents[1]

        # Original message
        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/reply",
                "sender_name": agent_a,
                "to": [agent_b],
                "subject": "Original",
                "body_md": "Original message.",
            },
        )
        orig_id = orig.data["deliveries"][0]["payload"]["id"]

        # Reply
        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": "/test/reply",
                "message_id": orig_id,
                "sender_name": agent_b,
                "body_md": "This is my reply.",
            },
        )

        assert "reply_to" in reply.data
        assert reply.data["reply_to"] == orig_id
        assert "thread_id" in reply.data


@pytest.mark.asyncio
async def test_reply_message_prefixes_subject(isolated_env):
    """reply_message should prefix subject with 'Re:'."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/re", count=2)
        agent_a, agent_b = agents[0], agents[1]

        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/re",
                "sender_name": agent_a,
                "to": [agent_b],
                "subject": "Discussion",
                "body_md": "Let's discuss.",
            },
        )
        orig_id = orig.data["deliveries"][0]["payload"]["id"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": "/test/re",
                "message_id": orig_id,
                "sender_name": agent_b,
                "body_md": "Sure!",
            },
        )

        delivery = reply.data["deliveries"][0]
        subject = delivery["payload"]["subject"]
        assert subject.lower().startswith("re:")


# ============================================================================
# Read and Acknowledgment Tests
# ============================================================================


@pytest.mark.asyncio
async def test_mark_message_read_workflow(isolated_env):
    """mark_message_read should mark the message as read for the recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/read", count=2)
        sender, receiver = agents[0], agents[1]

        send_result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/read",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Please Read",
                "body_md": "Important message.",
            },
        )
        msg_id = send_result.data["deliveries"][0]["payload"]["id"]

        # Mark as read
        read_result = await client.call_tool(
            "mark_message_read",
            {
                "project_key": "/test/read",
                "agent_name": receiver,
                "message_id": msg_id,
            },
        )

        assert read_result.data["read"] is True
        assert "read_at" in read_result.data
        assert read_result.data["read_at"] is not None


@pytest.mark.asyncio
async def test_acknowledge_message_workflow(isolated_env):
    """acknowledge_message should mark the message as acknowledged."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/ackflow", count=2)
        sender, receiver = agents[0], agents[1]

        send_result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/ackflow",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Needs Acknowledgment",
                "body_md": "Please acknowledge.",
                "ack_required": True,
            },
        )
        msg_id = send_result.data["deliveries"][0]["payload"]["id"]

        # Acknowledge
        ack_result = await client.call_tool(
            "acknowledge_message",
            {
                "project_key": "/test/ackflow",
                "agent_name": receiver,
                "message_id": msg_id,
            },
        )

        assert ack_result.data["acknowledged"] is True
        assert "acknowledged_at" in ack_result.data
        # Acknowledge should also mark as read
        assert "read_at" in ack_result.data


@pytest.mark.asyncio
async def test_read_and_ack_are_idempotent(isolated_env):
    """Multiple read/ack calls should not change timestamps."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/idempotent", count=2)
        sender, receiver = agents[0], agents[1]

        send_result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/idempotent",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Idempotent Test",
                "body_md": "Test.",
                "ack_required": True,
            },
        )
        msg_id = send_result.data["deliveries"][0]["payload"]["id"]

        # First read
        read1 = await client.call_tool(
            "mark_message_read",
            {"project_key": "/test/idempotent", "agent_name": receiver, "message_id": msg_id},
        )
        read1_at = read1.data["read_at"]

        # Second read (should return same timestamp)
        read2 = await client.call_tool(
            "mark_message_read",
            {"project_key": "/test/idempotent", "agent_name": receiver, "message_id": msg_id},
        )
        assert read2.data["read_at"] == read1_at

        # First ack
        ack1 = await client.call_tool(
            "acknowledge_message",
            {"project_key": "/test/idempotent", "agent_name": receiver, "message_id": msg_id},
        )
        ack1_at = ack1.data["acknowledged_at"]

        # Second ack (should return same timestamp)
        ack2 = await client.call_tool(
            "acknowledge_message",
            {"project_key": "/test/idempotent", "agent_name": receiver, "message_id": msg_id},
        )
        assert ack2.data["acknowledged_at"] == ack1_at


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_nonexistent_recipient_fails(isolated_env):
    """send_message to a non-existent recipient should fail."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/error", count=1)
        existing_agent = agents[0]

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/error",
                    "sender_name": existing_agent,
                    # Use a valid adjective+noun format that doesn't exist
                    "to": ["SilentGlacier"],
                    "subject": "Error Test",
                    "body_md": "This should fail.",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "not found" in error_msg or "not registered" in error_msg


@pytest.mark.asyncio
async def test_send_message_nonexistent_sender_fails(isolated_env):
    """send_message from a non-existent sender should fail."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/error2", count=1)
        receiver = agents[0]

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/error2",
                    # Use a valid adjective+noun format that doesn't exist
                    "sender_name": "QuietMountain",
                    "to": [receiver],
                    "subject": "Error Test",
                    "body_md": "This should fail.",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "not found" in error_msg or "not registered" in error_msg


@pytest.mark.asyncio
async def test_reply_to_nonexistent_message_fails(isolated_env):
    """reply_message to a non-existent message should fail."""
    server = build_mcp_server()
    async with Client(server) as client:
        agents = await setup_project_with_agents(client, "/test/error3", count=1)
        agent = agents[0]

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": "/test/error3",
                    "message_id": 999999,  # Non-existent
                    "sender_name": agent,
                    "body_md": "Reply to nothing.",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "not found" in error_msg or "does not exist" in error_msg or "invalid" in error_msg
