"""
Test suite for cross-project direct messaging without contact approval.

Tests verify that:
1. Agents in different projects can message each other directly
2. No contact approval is needed for cross-project communication
3. Cross-project messaging works bidirectionally
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_cross_project_direct_message(isolated_env):
    """Test that agents in different projects can message each other directly."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Create two separate projects
        await client.call_tool("ensure_project", {"human_key": "/project-frontend"})
        await client.call_tool("ensure_project", {"human_key": "/project-backend"})

        # Register agent in frontend project
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/project-frontend",
                "program": "frontend-dev",
                "model": "test-model",
                "name": "FrontendAgent",
            },
        )

        # Register agent in backend project
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/project-backend",
                "program": "backend-dev",
                "model": "test-model",
                "name": "BackendAgent",
            },
        )

        # FrontendAgent sends message to BackendAgent (cross-project)
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-frontend",
                "sender_name": "FrontendAgent",
                "to": ["BackendAgent"],
                "subject": "Cross-project message",
                "body_md": "Message from frontend to backend",
            },
        )

        # Should succeed without requiring contact approval
        assert result is not None
        assert result["count"] == 1


@pytest.mark.asyncio
async def test_cross_project_bidirectional_messaging(isolated_env):
    """Test bidirectional cross-project messaging works without approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Create two projects
        await client.call_tool("ensure_project", {"human_key": "/project-a"})
        await client.call_tool("ensure_project", {"human_key": "/project-b"})

        # Register agents
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-a", "program": "test", "model": "test", "name": "AgentA"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-b", "program": "test", "model": "test", "name": "AgentB"},
        )

        # A -> B
        result1 = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-a",
                "sender_name": "AgentA",
                "to": ["AgentB"],
                "subject": "From A to B",
                "body_md": "Hello from project A",
            },
        )
        assert result1["count"] == 1

        # B -> A (reverse direction, should work immediately without approval)
        result2 = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-b",
                "sender_name": "AgentB",
                "to": ["AgentA"],
                "subject": "From B to A",
                "body_md": "Hello from project B",
            },
        )
        assert result2["count"] == 1


@pytest.mark.asyncio
async def test_cross_project_multiple_recipients(isolated_env):
    """Test cross-project message to multiple recipients in different projects."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Create three projects
        await client.call_tool("ensure_project", {"human_key": "/project-1"})
        await client.call_tool("ensure_project", {"human_key": "/project-2"})
        await client.call_tool("ensure_project", {"human_key": "/project-3"})

        # Register sender in project-1
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-1", "program": "test", "model": "test", "name": "Sender"},
        )

        # Register recipients in different projects
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-2", "program": "test", "model": "test", "name": "Recipient1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-3", "program": "test", "model": "test", "name": "Recipient2"},
        )

        # Send to recipients in different projects
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-1",
                "sender_name": "Sender",
                "to": ["Recipient1", "Recipient2"],
                "subject": "Multi-project broadcast",
                "body_md": "Message to multiple projects",
            },
        )

        # Should succeed
        assert result is not None


@pytest.mark.asyncio
async def test_cross_project_first_contact_no_setup(isolated_env):
    """Test that first cross-project contact requires no setup or approval."""
    server = build_mcp_server()

    async with Client(server) as client:
        # Create completely isolated projects
        await client.call_tool("ensure_project", {"human_key": "/isolated-project-1"})
        await client.call_tool("ensure_project", {"human_key": "/isolated-project-2"})

        # Register agents that have never interacted
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/isolated-project-1",
                "program": "test",
                "model": "test",
                "name": "IsolatedAgent1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/isolated-project-2",
                "program": "test",
                "model": "test",
                "name": "IsolatedAgent2",
            },
        )

        # First ever cross-project message - should work immediately
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/isolated-project-1",
                "sender_name": "IsolatedAgent1",
                "to": ["IsolatedAgent2"],
                "subject": "First cross-project contact",
                "body_md": "No setup needed",
            },
        )

        assert result["count"] == 1


@pytest.mark.asyncio
async def test_cross_project_thread_continuation(isolated_env):
    """Test that cross-project messages can continue threads."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/project-x"})
        await client.call_tool("ensure_project", {"human_key": "/project-y"})

        await client.call_tool(
            "register_agent",
            {"project_key": "/project-x", "program": "test", "model": "test", "name": "AgentX"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/project-y", "program": "test", "model": "test", "name": "AgentY"},
        )

        # Start thread in project-x
        result1 = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-x",
                "sender_name": "AgentX",
                "to": ["AgentY"],
                "subject": "Thread starter",
                "body_md": "Starting a thread",
                "thread_id": "CROSS-PROJECT-123",
            },
        )
        assert result1["count"] == 1

        # Continue thread from project-y (cross-project thread continuation)
        result2 = await client.call_tool(
            "send_message",
            {
                "project_key": "/project-y",
                "sender_name": "AgentY",
                "to": ["AgentX"],
                "subject": "Re: Thread starter",
                "body_md": "Continuing the thread",
                "thread_id": "CROSS-PROJECT-123",
            },
        )
        assert result2["count"] == 1


@pytest.mark.asyncio
async def test_cross_project_rapid_succession(isolated_env):
    """Test rapid cross-project messaging without throttling."""
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/fast-project-1"})
        await client.call_tool("ensure_project", {"human_key": "/fast-project-2"})

        await client.call_tool(
            "register_agent",
            {"project_key": "/fast-project-1", "program": "test", "model": "test", "name": "FastSender"},
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/fast-project-2",
                "program": "test",
                "model": "test",
                "name": "FastReceiver",
            },
        )

        # Send multiple cross-project messages rapidly
        for i in range(3):
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/fast-project-1",
                    "sender_name": "FastSender",
                    "to": ["FastReceiver"],
                    "subject": f"Rapid message {i+1}",
                    "body_md": f"Content {i+1}",
                },
            )
            assert result["count"] == 1
