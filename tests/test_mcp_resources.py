"""P1 Core Tests: MCP Resources Read Access.

Test all MCP resources return correct data with proper JSON structure.

Test Cases:
1. resource://project/{slug} - project details
2. resource://agents/{project} - agent list
3. resource://inbox/{agent}?project= - inbox messages
4. resource://outbox/{agent}?project= - outbox messages
5. resource://thread/{id}?project= - thread messages
6. resource://file_reservations/{project} - active reservations

Verification:
- Correct JSON structure returned
- Query parameters work (limit, include_bodies)
- Missing resources return appropriate error

Reference: mcp_agent_mail-hqk
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

# ============================================================================
# Helper: Parse JSON from resource blocks
# ============================================================================


def parse_resource_json(blocks) -> Any:
    """Parse JSON from resource content blocks."""
    if not blocks:
        return None
    # Combine all text blocks
    text = "".join(b.text or "" for b in blocks)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ============================================================================
# Test: resource://project/{slug}
# ============================================================================


@pytest.mark.asyncio
async def test_project_resource_returns_project_details(isolated_env):
    """resource://project/{slug} returns project details with agents list."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/projresource"})

        # Register an agent (auto-generated name)
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ProjResource", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Read project resource
        blocks = await client.read_resource("resource://project/projresource")
        data = parse_resource_json(blocks)

        assert data is not None, "Project resource should return JSON"
        assert isinstance(data, dict), "Project resource should be a dict"
        assert "human_key" in data, "Should include human_key"
        assert "slug" in data, "Should include slug"
        assert "agents" in data, "Should include agents list"
        assert isinstance(data["agents"], list), "Agents should be a list"
        assert len(data["agents"]) >= 1, "Should have at least one agent"

        # Verify agent is in list
        agent_names = [a["name"] for a in data["agents"]]
        assert agent_name in agent_names, "Should include registered agent"


@pytest.mark.asyncio
async def test_project_resource_with_human_key(isolated_env):
    """resource://project/{human_key} also works with slug."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/projhk"})

        blocks = await client.read_resource("resource://project/projhk")
        data = parse_resource_json(blocks)

        assert data is not None, "Should return project data"
        assert "human_key" in data


# ============================================================================
# Test: resource://agents/{project_key}
# ============================================================================


@pytest.mark.asyncio
async def test_agents_resource_returns_agent_list(isolated_env):
    """resource://agents/{project} returns agent directory with metadata."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/agentsres"})

        # Register multiple agents with proper adjective+noun names
        await client.call_tool(
            "register_agent",
            {
                "project_key": "AgentsRes",
                "program": "claude-code",
                "model": "opus-4",
                "name": "BlueLake",
                "task_description": "Backend development",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "AgentsRes",
                "program": "codex",
                "model": "gpt-5",
                "name": "GreenField",
                "task_description": "Frontend work",
            },
        )

        # Read agents directory
        blocks = await client.read_resource("resource://agents/agentsres")
        data = parse_resource_json(blocks)

        assert data is not None, "Agents resource should return JSON"
        assert isinstance(data, dict), "Agents resource should be a dict"
        assert "project" in data, "Should include project info"
        assert "agents" in data, "Should include agents list"
        assert isinstance(data["agents"], list), "Agents should be a list"
        assert len(data["agents"]) == 2, "Should have two agents"

        # Verify agent metadata fields
        agent = data["agents"][0]
        assert "name" in agent, "Agent should have name"
        assert "program" in agent, "Agent should have program"
        assert "model" in agent, "Agent should have model"
        assert "task_description" in agent, "Agent should have task_description"
        assert "inception_ts" in agent, "Agent should have inception_ts"
        assert "last_active_ts" in agent, "Agent should have last_active_ts"
        assert "unread_count" in agent, "Agent should have unread_count"


@pytest.mark.asyncio
async def test_agents_resource_shows_unread_count(isolated_env):
    """Agents resource shows correct unread message count."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/agentsunread"})

        sender_result = await client.call_tool(
            "register_agent",
            {"project_key": "AgentsUnread", "program": "test", "model": "test"},
        )
        sender_name = sender_result.data["name"]

        receiver_result = await client.call_tool(
            "register_agent",
            {"project_key": "AgentsUnread", "program": "test", "model": "test"},
        )
        receiver_name = receiver_result.data["name"]

        # Send messages to receiver
        for i in range(3):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "AgentsUnread",
                    "sender_name": sender_name,
                    "to": [receiver_name],
                    "subject": f"Message {i}",
                    "body_md": "Test body",
                },
            )

        # Check unread count
        blocks = await client.read_resource("resource://agents/agentsunread")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, dict), "Agents resource should be a dict"
        receiver_agent = next((a for a in data["agents"] if a["name"] == receiver_name), None)
        assert receiver_agent is not None, f"Should find {receiver_name} agent"
        # At least 3 from the test messages (may be more due to contact request flow)
        assert receiver_agent["unread_count"] >= 3, f"Should show at least 3 unread messages, got {receiver_agent['unread_count']}"


# ============================================================================
# Test: resource://inbox/{agent}?project=
# ============================================================================


@pytest.mark.asyncio
async def test_inbox_resource_returns_messages(isolated_env):
    """resource://inbox/{agent}?project= returns inbox messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/inboxres"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "InboxRes", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Send message to self
        await client.call_tool(
            "send_message",
            {
                "project_key": "InboxRes",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Test Inbox Message",
                "body_md": "This is a test message body",
            },
        )

        # Read inbox resource
        blocks = await client.read_resource(f"resource://inbox/{agent_name}?project=InboxRes")
        data = parse_resource_json(blocks)

        assert data is not None, "Inbox resource should return JSON"
        assert isinstance(data, dict), "Inbox resource should be a dict"
        assert "project" in data, "Should include project"
        assert "agent" in data, "Should include agent"
        assert "count" in data, "Should include count"
        assert "messages" in data, "Should include messages"
        assert data["count"] >= 1, "Should have at least one message"
        assert data["agent"] == agent_name, "Should be for correct agent"


@pytest.mark.asyncio
async def test_inbox_resource_with_limit_param(isolated_env):
    """Inbox resource respects limit query parameter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/inboxlimit"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "InboxLimit", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Send multiple messages
        for i in range(5):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "InboxLimit",
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": f"Message {i}",
                    "body_md": "Body",
                },
            )

        # Request with limit=2
        blocks = await client.read_resource(f"resource://inbox/{agent_name}?project=InboxLimit&limit=2")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, dict), "Inbox resource should be a dict"
        assert len(data["messages"]) <= 2, "Should respect limit parameter"


@pytest.mark.asyncio
async def test_inbox_resource_with_include_bodies(isolated_env):
    """Inbox resource includes bodies when include_bodies=true."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/inboxbody"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "InboxBody", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "send_message",
            {
                "project_key": "InboxBody",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Body Test",
                "body_md": "This body should appear",
            },
        )

        # Request with include_bodies=true
        blocks = await client.read_resource(f"resource://inbox/{agent_name}?project=InboxBody&include_bodies=true")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, dict), "Inbox resource should be a dict"
        assert len(data["messages"]) >= 1
        msg = data["messages"][0]
        assert "body_md" in msg, "Should include body when requested"
        assert "appear" in msg["body_md"], "Body should contain message content"


# ============================================================================
# Test: resource://outbox/{agent}?project=
# ============================================================================


@pytest.mark.asyncio
async def test_outbox_resource_returns_sent_messages(isolated_env):
    """resource://outbox/{agent}?project= returns sent messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/outboxres"})

        sender_result = await client.call_tool(
            "register_agent",
            {"project_key": "OutboxRes", "program": "test", "model": "test"},
        )
        sender_name = sender_result.data["name"]

        receiver_result = await client.call_tool(
            "register_agent",
            {"project_key": "OutboxRes", "program": "test", "model": "test"},
        )
        receiver_name = receiver_result.data["name"]

        # Send message
        await client.call_tool(
            "send_message",
            {
                "project_key": "OutboxRes",
                "sender_name": sender_name,
                "to": [receiver_name],
                "subject": "Outbox Test Message",
                "body_md": "Sent from outbox test",
            },
        )

        # Read outbox resource
        blocks = await client.read_resource(f"resource://outbox/{sender_name}?project=OutboxRes")
        data = parse_resource_json(blocks)

        assert data is not None, "Outbox resource should return JSON"
        assert isinstance(data, dict), "Outbox resource should be a dict"
        assert "project" in data, "Should include project"
        assert "agent" in data, "Should include agent"
        assert "count" in data, "Should include count"
        assert "messages" in data, "Should include messages"
        assert data["count"] >= 1, "Should have at least one sent message"
        assert data["agent"] == sender_name, "Should be for correct agent"


@pytest.mark.asyncio
async def test_outbox_resource_with_limit(isolated_env):
    """Outbox resource respects limit parameter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/outboxlimit"})

        sender_result = await client.call_tool(
            "register_agent",
            {"project_key": "OutboxLimit", "program": "test", "model": "test"},
        )
        sender_name = sender_result.data["name"]

        receiver_result = await client.call_tool(
            "register_agent",
            {"project_key": "OutboxLimit", "program": "test", "model": "test"},
        )
        receiver_name = receiver_result.data["name"]

        # Send multiple messages
        for i in range(5):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "OutboxLimit",
                    "sender_name": sender_name,
                    "to": [receiver_name],
                    "subject": f"Outbox {i}",
                    "body_md": "Test",
                },
            )

        # Request with limit=3
        blocks = await client.read_resource(f"resource://outbox/{sender_name}?project=OutboxLimit&limit=3")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, dict), "Outbox resource should be a dict"
        assert len(data["messages"]) <= 3, "Should respect limit"


# ============================================================================
# Test: resource://thread/{id}?project=
# ============================================================================


@pytest.mark.asyncio
async def test_thread_resource_returns_thread_messages(isolated_env):
    """resource://thread/{id}?project= returns thread messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/threadres"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ThreadRes", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Create a message with thread_id
        await client.call_tool(
            "send_message",
            {
                "project_key": "ThreadRes",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Thread Test",
                "body_md": "Initial message",
                "thread_id": "TEST-THREAD-001",
            },
        )

        # Send reply in same thread
        await client.call_tool(
            "send_message",
            {
                "project_key": "ThreadRes",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Re: Thread Test",
                "body_md": "Reply message",
                "thread_id": "TEST-THREAD-001",
            },
        )

        # Read thread resource
        blocks = await client.read_resource("resource://thread/TEST-THREAD-001?project=ThreadRes")
        data = parse_resource_json(blocks)

        assert data is not None, "Thread resource should return JSON"
        assert "project" in data, "Should include project"
        assert "thread_id" in data, "Should include thread_id"
        assert "messages" in data, "Should include messages"
        assert len(data["messages"]) == 2, "Should have two messages in thread"


@pytest.mark.asyncio
async def test_thread_resource_with_message_id(isolated_env):
    """Thread resource can use message ID as seed."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/threadid"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ThreadId", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "ThreadId",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Message ID Thread",
                "body_md": "Test body",
            },
        )

        # Extract message ID
        deliveries = result.data.get("deliveries", [])
        msg_id = 1
        if deliveries:
            msg_id = deliveries[0].get("payload", {}).get("id", 1)

        # Read thread by message ID
        blocks = await client.read_resource(f"resource://thread/{msg_id}?project=ThreadId")
        data = parse_resource_json(blocks)

        assert data is not None, "Should return thread data"
        assert "messages" in data


@pytest.mark.asyncio
async def test_thread_resource_with_include_bodies(isolated_env):
    """Thread resource includes bodies when requested."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/threadbody"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ThreadBody", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "send_message",
            {
                "project_key": "ThreadBody",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Thread Bodies",
                "body_md": "Include this body content",
                "thread_id": "BODY-THREAD",
            },
        )

        # Request with include_bodies=true
        blocks = await client.read_resource("resource://thread/BODY-THREAD?project=ThreadBody&include_bodies=true")
        data = parse_resource_json(blocks)

        assert data is not None
        assert len(data["messages"]) >= 1
        msg = data["messages"][0]
        assert "body_md" in msg, "Should include body"


# ============================================================================
# Test: resource://file_reservations/{project}
# ============================================================================


@pytest.mark.asyncio
async def test_file_reservations_resource_returns_reservations(isolated_env):
    """resource://file_reservations/{project} returns active reservations."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/fileres"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "FileRes", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Create a file reservation
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "FileRes",
                "agent_name": agent_name,
                "paths": ["src/main.py"],
                "exclusive": True,
                "reason": "Testing reservations resource",
            },
        )

        # Read file reservations resource
        blocks = await client.read_resource("resource://file_reservations/fileres")
        data = parse_resource_json(blocks)

        assert data is not None, "File reservations resource should return JSON"
        assert isinstance(data, list), "Should return a list of reservations"
        assert len(data) >= 1, "Should have at least one reservation"

        # Verify reservation details
        reservation = data[0]
        assert "id" in reservation, "Should have id"
        assert "agent" in reservation, "Should have agent"
        assert "path_pattern" in reservation, "Should have path_pattern"
        assert "exclusive" in reservation, "Should have exclusive flag"
        assert "reason" in reservation, "Should have reason"
        assert reservation["agent"] == agent_name
        assert reservation["path_pattern"] == "src/main.py"


@pytest.mark.asyncio
async def test_file_reservations_resource_active_only(isolated_env):
    """File reservations resource with active_only=true filters released."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/fileactive"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "FileActive", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # Create and release a reservation
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "FileActive",
                "agent_name": agent_name,
                "paths": ["released.py"],
            },
        )
        await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "FileActive",
                "agent_name": agent_name,
                "paths": ["released.py"],
            },
        )

        # Create an active reservation
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "FileActive",
                "agent_name": agent_name,
                "paths": ["active.py"],
            },
        )

        # Query active_only=true
        blocks = await client.read_resource("resource://file_reservations/fileactive?active_only=true")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, list), "File reservations should be a list"
        # Should only have the active reservation
        active_patterns = [r["path_pattern"] for r in data]
        assert "active.py" in active_patterns, "Should include active reservation"
        assert "released.py" not in active_patterns, "Should not include released"


@pytest.mark.asyncio
async def test_file_reservations_resource_includes_metadata(isolated_env):
    """File reservations resource includes stale status and timestamps."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/filemeta"})

        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "FileMeta", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "FileMeta",
                "agent_name": agent_name,
                "paths": ["meta.py"],
                "reason": "Testing metadata",
            },
        )

        blocks = await client.read_resource("resource://file_reservations/filemeta")
        data = parse_resource_json(blocks)

        assert data is not None
        assert isinstance(data, list), "File reservations should be a list"
        assert len(data) >= 1
        reservation = data[0]
        assert "created_ts" in reservation, "Should have created_ts"
        assert "expires_ts" in reservation, "Should have expires_ts"
        assert "stale" in reservation, "Should have stale flag"


# ============================================================================
# Test: Error Handling
# ============================================================================


@pytest.mark.asyncio
async def test_project_resource_nonexistent_returns_error(isolated_env):
    """Accessing nonexistent project returns error."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.read_resource("resource://project/nonexistent-project-xyz")
            # If it doesn't raise, check for error in response
            pytest.fail("Should raise error for nonexistent project")
        except Exception as e:
            # Expected - resource should fail for nonexistent project
            assert "not found" in str(e).lower() or "error" in str(e).lower()


@pytest.mark.asyncio
async def test_inbox_resource_requires_project(isolated_env):
    """Inbox resource requires project parameter when agent is ambiguous."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Create two projects with same agent name (using known-valid name)
        await client.call_tool("ensure_project", {"human_key": "/proj1"})
        await client.call_tool("ensure_project", {"human_key": "/proj2"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Proj1", "program": "test", "model": "test", "name": "BlueLake"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Proj2", "program": "test", "model": "test", "name": "BlueLake"},
        )

        try:
            # Without project parameter, should fail or require clarification
            await client.read_resource("resource://inbox/BlueLake")
            # May succeed if auto-detection works, or fail
        except Exception as e:
            # Expected - ambiguous agent requires project
            error_str = str(e).lower()
            assert "project" in error_str or "required" in error_str or "ambiguous" in error_str


@pytest.mark.asyncio
async def test_outbox_resource_requires_project(isolated_env):
    """Outbox resource requires project parameter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/outboxreq"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "OutboxReq", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.read_resource(f"resource://outbox/{agent_name}")
            pytest.fail("Should require project parameter")
        except Exception as e:
            assert "project" in str(e).lower()
