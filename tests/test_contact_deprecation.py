"""
Test suite for verifying contact requirement deprecation.

Tests verify that:
1. Contact-related tools are deprecated/removed
2. Contact policies (if they exist) don't block messages
3. Old contact-based workflows are no longer needed
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_contact_tools_are_deprecated(isolated_env):
    """Test that contact-related tools are no longer available."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Get list of available tools
        tools = await client.list_tools()
        tool_names = [tool.name for tool in tools]

        # Verify deprecated contact tools are not in the list
        deprecated_tools = [
            "request_contact",
            "respond_contact",
            "list_contacts",
            "set_contact_policy",
            "macro_contact_handshake",
        ]

        for tool_name in deprecated_tools:
            assert tool_name not in tool_names, f"Deprecated tool '{tool_name}' should not be available"


@pytest.mark.asyncio
async def test_messaging_works_regardless_of_contact_policy_setting(isolated_env):
    """Test that messages are delivered even if contact_policy field exists in DB."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register agents
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Sender"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Receiver"},
        )

        # Note: Even if contact_policy field exists in the database,
        # the send_message function should ignore it and deliver the message

        # Send message
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Sender",
                "to": ["Receiver"],
                "subject": "Test message",
                "body_md": "Should be delivered regardless of any policy",
            },
        )

        # Message should be delivered successfully
        assert result["count"] == 1

        # Verify delivery
        inbox = await client.call_tool(
            "fetch_inbox",
            {"project_key": "/test-project", "agent_name": "Receiver", "limit": 10},
        )
        assert len(inbox) == 1


@pytest.mark.asyncio
async def test_no_contact_required_error_thrown(isolated_env):
    """Test that CONTACT_REQUIRED error is never thrown."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register two agents who have never interacted
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "NewAgent1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "NewAgent2"},
        )

        # First message should not throw CONTACT_REQUIRED error
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "NewAgent1",
                "to": ["NewAgent2"],
                "subject": "First message",
                "body_md": "Should not require contact approval",
            },
        )

        assert result["count"] == 1
        # No CONTACT_REQUIRED error should be thrown


@pytest.mark.asyncio
async def test_no_contact_blocked_error_thrown(isolated_env):
    """Test that CONTACT_BLOCKED error is never thrown."""
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

        # Even if there was a "block_all" policy in the past, it should be ignored
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Sender",
                "to": ["Receiver"],
                "subject": "Test message",
                "body_md": "Should be delivered",
            },
        )

        assert result["count"] == 1
        # No CONTACT_BLOCKED error should be thrown


@pytest.mark.asyncio
async def test_agent_link_table_not_consulted(isolated_env):
    """Test that messaging works without consulting agent_links table."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register multiple agents
        for name in ["Agent1", "Agent2", "Agent3"]:
            await client.call_tool(
                "register_agent",
                {"project_key": "/test-project", "program": "test", "model": "test", "name": name},
            )

        # All agents should be able to message each other without any links
        pairs = [("Agent1", "Agent2"), ("Agent2", "Agent3"), ("Agent3", "Agent1")]

        for sender, receiver in pairs:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test-project",
                    "sender_name": sender,
                    "to": [receiver],
                    "subject": f"From {sender} to {receiver}",
                    "body_md": "No link required",
                },
            )
            assert result["count"] == 1


@pytest.mark.asyncio
async def test_auto_contact_if_blocked_parameter_ignored(isolated_env):
    """Test that auto_contact_if_blocked parameter is ignored (deprecated)."""
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

        # Try sending with auto_contact_if_blocked parameter (deprecated)
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Sender",
                "to": ["Receiver"],
                "subject": "Test",
                "body_md": "Testing deprecated parameter",
                "auto_contact_if_blocked": False,  # This parameter should be ignored
            },
        )

        # Message should still be delivered (parameter is ignored)
        assert result["count"] == 1


@pytest.mark.asyncio
async def test_no_contact_ttl_restrictions(isolated_env):
    """Test that there are no TTL restrictions for messaging."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Agent1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "Agent2"},
        )

        # Send message - should work immediately without any TTL checks
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "Agent1",
                "to": ["Agent2"],
                "subject": "No TTL check",
                "body_md": "Immediate delivery",
            },
        )

        assert result["count"] == 1


@pytest.mark.asyncio
async def test_no_prior_reservation_overlap_required(isolated_env):
    """Test that messages work without prior file reservation overlap."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "DevA"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/test-project", "program": "test", "model": "test", "name": "DevB"},
        )

        # No file reservations created - messaging should still work
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "DevA",
                "to": ["DevB"],
                "subject": "No reservation needed",
                "body_md": "Can message without shared file reservations",
            },
        )

        assert result["count"] == 1


@pytest.mark.asyncio
async def test_no_thread_participation_required(isolated_env):
    """Test that messaging works without prior thread participation."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test-project"})

        # Register three agents
        for name in ["InitiatorAgent", "ParticipantAgent", "OutsiderAgent"]:
            await client.call_tool(
                "register_agent",
                {"project_key": "/test-project", "program": "test", "model": "test", "name": name},
            )

        # InitiatorAgent and ParticipantAgent have a thread
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "InitiatorAgent",
                "to": ["ParticipantAgent"],
                "subject": "Thread discussion",
                "body_md": "Starting a thread",
                "thread_id": "THREAD-123",
            },
        )

        # OutsiderAgent (not in thread) can still message ParticipantAgent
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test-project",
                "sender_name": "OutsiderAgent",
                "to": ["ParticipantAgent"],
                "subject": "Separate message",
                "body_md": "Not part of the thread, but can still message",
            },
        )

        assert result["count"] == 1
