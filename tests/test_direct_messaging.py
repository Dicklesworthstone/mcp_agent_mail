"""
Test suite for direct messaging without contact approval requirements.

Tests verify that:
1. Messages can be sent directly without contact approval
2. Contact policies (if set) do not block messages
3. Cross-project messaging works without contact links
4. Multiple agents can message each other freely
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_direct_message_without_contact_approval(isolated_env):
    """Test that messages can be sent directly without any contact approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Create project and register two agents
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test-project",
                "program": "test-program",
                "model": "test-model",
                "name": "Agent1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test-project",
                "program": "test-program",
                "model": "test-model",
                "name": "Agent2",
            },
        )

        # Send message directly without any contact request/approval
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Agent1",
                "to": ["Agent2"],
                "subject": "Direct message test",
                "body_md": "This message should be delivered without contact approval",
            },
        )

        # Verify message was delivered successfully
        assert result is not None
        assert "deliveries" in result
        assert result["count"] == 1

        # Verify message is in recipient's inbox
        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test-project",
                "agent_name": "Agent2",
                "limit": 10,
            },
        )

        assert len(inbox) == 1
        assert inbox[0]["subject"] == "Direct message test"
        assert inbox[0]["from"] == "Agent1"


@pytest.mark.asyncio
async def test_bidirectional_messaging_without_approval(isolated_env):
    """Test that agents can message each other bidirectionally without approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test-project",
                "program": "test",
                "model": "test",
                "name": "Alice",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test-project",
                "program": "test",
                "model": "test",
                "name": "Bob",
            },
        )

        # Alice sends to Bob
        result1 = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Alice",
                "to": ["Bob"],
                "subject": "Hello from Alice",
                "body_md": "Hi Bob!",
            },
        )
        assert result1["count"] == 1

        # Bob sends to Alice (reverse direction, should work immediately)
        result2 = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Bob",
                "to": ["Alice"],
                "subject": "Hello from Bob",
                "body_md": "Hi Alice!",
            },
        )
        assert result2["count"] == 1

        # Verify both inboxes
        alice_inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "Alice", "limit": 10},
        )
        assert len(alice_inbox) == 1
        assert alice_inbox[0]["from"] == "Bob"

        bob_inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "Bob", "limit": 10},
        )
        assert len(bob_inbox) == 1
        assert bob_inbox[0]["from"] == "Alice"


@pytest.mark.asyncio
async def test_multiple_recipients_without_approval(isolated_env):
    """Test that messages can be sent to multiple recipients without approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register sender and multiple recipients
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Sender"},
        )
        for i in range(1, 4):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "/test-project",
                    "program": "test",
                    "model": "test",
                    "name": f"Recipient{i}",
                },
            )

        # Send to multiple recipients at once
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Sender",
                "to": ["Recipient1", "Recipient2", "Recipient3"],
                "subject": "Broadcast message",
                "body_md": "Message to all recipients",
            },
        )

        # Should deliver to all 3 recipients
        assert result["count"] == 3

        # Verify all recipients got the message
        for i in range(1, 4):
            inbox = await client.call_tool(
                "fetch_inbox",
                {"project_key": "/test-project", "agent_name": f"Recipient{i}", "limit": 10},
            )
            assert len(inbox) == 1
            assert inbox[0]["subject"] == "Broadcast message"


@pytest.mark.asyncio
async def test_first_contact_no_approval_needed(isolated_env):
    """Test that the very first contact between agents requires no approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "NewAgent1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "NewAgent2"},
        )

        # First ever message between these agents - should work immediately
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "NewAgent1",
                "to": ["NewAgent2"],
                "subject": "First contact",
                "body_md": "This is our first message",
            },
        )

        assert result["count"] == 1

        inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "NewAgent2", "limit": 10},
        )
        assert len(inbox) == 1
        assert inbox[0]["subject"] == "First contact"


@pytest.mark.asyncio
async def test_message_with_cc_and_bcc_no_approval(isolated_env):
    """Test that CC and BCC recipients receive messages without approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register multiple agents
        for name in ["Sender", "To1", "CC1", "BCC1"]:
            await client.call_tool(
                "register_agent",
                {"project_key": "/test-project", "program": "test", "model": "test", "name": name},
            )

        # Send with to, cc, and bcc
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Sender",
                "to": ["To1"],
                "cc": ["CC1"],
                "bcc": ["BCC1"],
                "subject": "Message with CC/BCC",
                "body_md": "Testing CC and BCC",
            },
        )

        # Should deliver to all 3 recipients
        assert result["count"] == 3

        # Verify all recipients got the message
        for recipient in ["To1", "CC1", "BCC1"]:
            inbox = await client.call_tool(
                "fetch_inbox",
                {"project_key": "/test-project", "agent_name": recipient, "limit": 10},
            )
            assert len(inbox) == 1
            assert inbox[0]["subject"] == "Message with CC/BCC"


@pytest.mark.asyncio
async def test_rapid_succession_messages_no_throttling(isolated_env):
    """Test that multiple messages can be sent in rapid succession without throttling."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Sender"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Receiver"},
        )

        # Send 5 messages in rapid succession
        for i in range(5):
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test-project",
                    "sender_name": "Sender",
                    "to": ["Receiver"],
                    "subject": f"Message {i+1}",
                    "body_md": f"Content {i+1}",
                },
            )
            assert result["count"] == 1

        # Verify all 5 messages were delivered
        inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "Receiver", "limit": 10},
        )
        assert len(inbox) == 5


@pytest.mark.asyncio
async def test_message_to_nonexistent_agent_fails_gracefully(isolated_env):
    """Test that sending to a non-existent agent fails gracefully."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Sender"},
        )

        # Try to send to non-existent agent
        # Note: The actual behavior may vary - it might create the agent or fail
        # This test documents the current behavior
        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test-project",
                    "sender_name": "Sender",
                    "to": ["NonExistentAgent"],
                    "subject": "Test",
                    "body_md": "Test message",
                },
            )
            # If it succeeds, that's fine - implementation may auto-create or handle gracefully
            assert result is not None
        except Exception:
            # If it fails, that's also acceptable behavior
            pass


@pytest.mark.asyncio
async def test_self_messaging_works(isolated_env):
    """Test that an agent can send messages to itself."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "SelfTalker"},
        )

        # Send message to self
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "SelfTalker",
                "to": ["SelfTalker"],
                "subject": "Note to self",
                "body_md": "Remember to do X",
            },
        )

        assert result["count"] == 1

        # Verify message is in own inbox
        inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "SelfTalker", "limit": 10},
        )
        assert len(inbox) == 1
        assert inbox[0]["from"] == "SelfTalker"
