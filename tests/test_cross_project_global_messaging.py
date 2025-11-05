"""Comprehensive test for cross-project messaging and global agent discovery.

This test demonstrates:
1. Agents CAN communicate across projects using AgentLink
2. Agents can be discovered globally across all projects by name
3. Project keys can be any string (not just directory paths)
4. Messages can be sent to all agents with the same name across projects
"""

import pytest
from mcp_agent_mail.app import mcp


@pytest.mark.asyncio
async def test_global_agent_discovery_and_messaging():
    """Test that agents with the same name in different projects can all receive messages."""

    # Setup: Create 3 different projects with custom project keys (not directory paths)
    project_a = await mcp.call_tool("ensure_project", {
        "human_key": "/custom/project/alpha"
    })
    project_b = await mcp.call_tool("ensure_project", {
        "human_key": "/custom/project/beta"
    })
    project_c = await mcp.call_tool("ensure_project", {
        "human_key": "/custom/project/gamma"
    })

    # Register agents with the SAME NAME across different projects
    # This demonstrates that agent names are unique per project, not globally
    await mcp.call_tool("register_agent", {
        "project_key": "/custom/project/alpha",
        "agent_name": "DevAgent",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    await mcp.call_tool("register_agent", {
        "project_key": "/custom/project/beta",
        "agent_name": "DevAgent",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    await mcp.call_tool("register_agent", {
        "project_key": "/custom/project/gamma",
        "agent_name": "DevAgent",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    # Register a coordinator agent in project alpha
    await mcp.call_tool("register_agent", {
        "project_key": "/custom/project/alpha",
        "agent_name": "Coordinator",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    # Establish cross-project contact links
    # Coordinator → DevAgent in Beta
    req1 = await mcp.call_tool("request_contact", {
        "project_key": "/custom/project/alpha",
        "from_agent": "Coordinator",
        "to_agent": "DevAgent",
        "to_project": "/custom/project/beta",
        "reason": "Cross-project coordination"
    })
    assert req1["status"] == "pending"

    await mcp.call_tool("respond_contact", {
        "project_key": "/custom/project/beta",
        "to_agent": "DevAgent",
        "from_agent": "Coordinator",
        "from_project": "/custom/project/alpha",
        "accept": True
    })

    # Coordinator → DevAgent in Gamma
    req2 = await mcp.call_tool("request_contact", {
        "project_key": "/custom/project/alpha",
        "from_agent": "Coordinator",
        "to_agent": "DevAgent",
        "to_project": "/custom/project/gamma",
        "reason": "Cross-project coordination"
    })
    assert req2["status"] == "pending"

    await mcp.call_tool("respond_contact", {
        "project_key": "/custom/project/gamma",
        "to_agent": "DevAgent",
        "from_agent": "Coordinator",
        "from_project": "/custom/project/alpha",
        "accept": True
    })

    # Now send a message from Coordinator to ALL DevAgent instances
    result = await mcp.call_tool("send_message", {
        "project_key": "/custom/project/alpha",
        "sender_name": "Coordinator",
        "to": [
            "DevAgent",  # Local DevAgent in project alpha
            "project:/custom/project/beta#DevAgent",  # Explicit cross-project
            "project:/custom/project/gamma#DevAgent"  # Explicit cross-project
        ],
        "subject": "Global Announcement",
        "body_md": "This message reaches all DevAgent instances across all projects!",
        "importance": "high"
    })

    # Verify deliveries to all 3 projects
    deliveries = result.get("deliveries", [])
    assert len(deliveries) == 3, f"Expected 3 deliveries, got {len(deliveries)}"

    # Check each project received the message
    project_keys = {d["project"] for d in deliveries}
    assert "/custom/project/alpha" in project_keys or "alpha" in {d["project"].lower() for d in deliveries}
    assert any("beta" in d["project"].lower() for d in deliveries)
    assert any("gamma" in d["project"].lower() for d in deliveries)

    # Verify each DevAgent sees the message in their inbox
    for project_key in ["/custom/project/alpha", "/custom/project/beta", "/custom/project/gamma"]:
        inbox = await mcp.call_tool("fetch_inbox", {
            "project_key": project_key,
            "agent_name": "DevAgent"
        })

        messages = inbox["messages"]
        assert len(messages) >= 1, f"DevAgent in {project_key} should have received the message"

        # Find the global announcement
        announcement = next((m for m in messages if m["subject"] == "Global Announcement"), None)
        assert announcement is not None, f"Global announcement not found in {project_key}"
        assert announcement["from"] == "Coordinator"
        assert announcement["importance"] == "high"


@pytest.mark.asyncio
async def test_agent_name_not_primary_key_scoped_per_project():
    """Verify that agent names are unique per project, not globally.

    This means:
    - Same agent name CAN exist in multiple projects
    - Agent identity is (project_id, name) composite key
    - Cannot use agent name alone as global primary key
    """

    # Create two projects
    await mcp.call_tool("ensure_project", {"human_key": "/test/project1"})
    await mcp.call_tool("ensure_project", {"human_key": "/test/project2"})

    # Register agent with same name in both projects - should succeed
    agent1 = await mcp.call_tool("register_agent", {
        "project_key": "/test/project1",
        "agent_name": "CommonName",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    agent2 = await mcp.call_tool("register_agent", {
        "project_key": "/test/project2",
        "agent_name": "CommonName",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    # Both should succeed with different internal IDs
    assert agent1["name"] == "CommonName"
    assert agent2["name"] == "CommonName"
    # They are different agents in different projects
    assert agent1.get("project_slug") != agent2.get("project_slug")


@pytest.mark.asyncio
async def test_custom_project_keys_independent_of_directory():
    """Verify that project keys can be any string, not tied to actual directories.

    This means:
    - Project keys are logical identifiers
    - They don't need to match the agent's working directory
    - Useful for organizing agents conceptually rather than by filesystem location
    """

    # Create projects with non-directory-like keys
    custom_projects = [
        "/mycompany/team-alpha",
        "/mycompany/team-beta",
        "/client:acme/backend",
        "/namespace::feature-x"
    ]

    for key in custom_projects:
        result = await mcp.call_tool("ensure_project", {"human_key": key})
        assert result["human_key"] == key

        # Register an agent in each custom project
        agent = await mcp.call_tool("register_agent", {
            "project_key": key,
            "agent_name": "TestAgent",
            "program": "test",
            "model": "test-model"
        })
        assert agent["name"] == "TestAgent"


@pytest.mark.asyncio
async def test_cross_project_reply_with_case_insensitive_lookup():
    """Test that cross-project replies work with case-insensitive agent name lookup.

    This combines:
    - Cross-project messaging
    - Case-insensitive name resolution
    - Reply message threading
    """

    # Setup two projects
    await mcp.call_tool("ensure_project", {"human_key": "/projectX"})
    await mcp.call_tool("ensure_project", {"human_key": "/projectY"})

    # Register agents with mixed case names
    await mcp.call_tool("register_agent", {
        "project_key": "/projectX",
        "agent_name": "AliceAgent",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    await mcp.call_tool("register_agent", {
        "project_key": "/projectY",
        "agent_name": "BobAgent",
        "program": "claude-code",
        "model": "sonnet-4.5"
    })

    # Establish bidirectional contact
    await mcp.call_tool("request_contact", {
        "project_key": "/projectX",
        "from_agent": "AliceAgent",
        "to_agent": "BobAgent",
        "to_project": "/projectY"
    })

    await mcp.call_tool("respond_contact", {
        "project_key": "/projectY",
        "to_agent": "BobAgent",
        "from_agent": "AliceAgent",
        "from_project": "/projectX",
        "accept": True
    })

    # Reverse direction
    await mcp.call_tool("request_contact", {
        "project_key": "/projectY",
        "from_agent": "BobAgent",
        "to_agent": "AliceAgent",
        "to_project": "/projectX"
    })

    await mcp.call_tool("respond_contact", {
        "project_key": "/projectX",
        "to_agent": "AliceAgent",
        "from_agent": "BobAgent",
        "from_project": "/projectY",
        "accept": True
    })

    # Alice sends message to Bob using explicit cross-project syntax
    original = await mcp.call_tool("send_message", {
        "project_key": "/projectX",
        "sender_name": "AliceAgent",
        "to": ["project:/projectY#BobAgent"],
        "subject": "Cross-project coordination",
        "body_md": "Let's coordinate on the API design"
    })

    message_id = original["message_id"]
    thread_id = original["thread_id"]

    # Bob replies using LOWERCASE name (testing case-insensitive lookup)
    reply = await mcp.call_tool("reply_message", {
        "project_key": "/projectY",
        "sender_name": "BobAgent",
        "message_id": message_id,
        "to": ["aliceagent"],  # lowercase - should resolve to AliceAgent
        "body_md": "Sounds good! I agree with the approach."
    })

    assert reply["thread_id"] == thread_id

    # Verify Alice received the reply (case-insensitive should have worked)
    alice_inbox = await mcp.call_tool("fetch_inbox", {
        "project_key": "/projectX",
        "agent_name": "AliceAgent"
    })

    reply_msg = next((m for m in alice_inbox["messages"] if m["id"] == reply["message_id"]), None)
    assert reply_msg is not None, "Alice should have received Bob's reply (case-insensitive lookup)"
    assert reply_msg["from"] == "BobAgent"
    assert reply_msg["thread_id"] == thread_id
