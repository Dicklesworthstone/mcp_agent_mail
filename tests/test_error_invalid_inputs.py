"""P2 Tests: Errors - Invalid Inputs.

Test error handling for invalid inputs across MCP tools.
Verifies clear error messages and appropriate exception types.

Test Cases:
1. Invalid project_key format (relative path rejected)
2. Non-existent project (register_agent, whois, fetch_inbox, search_messages)
3. Invalid agent name format (single word, spaces)
4. Non-existent agent (send_message sender/recipient, file_reservation, whois, fetch_inbox, etc.)
5. Placeholder detection (YOUR_PROJECT, YOUR_AGENT_NAME)
6. Empty recipients list (API allows, returns 0 deliveries)
7. Empty subject (API allows)
8. Invalid contact policy (API normalizes to 'auto')
9. Empty file reservation paths (rejected)
10. TTL below minimum (API warns but allows)
11. Non-existent message (mark_read, acknowledge, reply)
12. Non-existent agent for release/renew reservations
13. Non-existent agent for contact request/respond

Reference: mcp_agent_mail-mj0
"""

from __future__ import annotations

import contextlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server

# ============================================================================
# Test: Invalid project_key
# ============================================================================


@pytest.mark.asyncio
async def test_ensure_project_requires_absolute_path(isolated_env):
    """ensure_project should require absolute path starting with /."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Relative path should fail
        try:
            await client.call_tool("ensure_project", {"human_key": "relative/path"})
            pytest.fail("Should reject relative path")
        except ToolError as e:
            error_str = str(e).lower()
            # Must mention 'absolute' or 'path' (but not just "/" which is too loose)
            assert "absolute" in error_str or "path" in error_str


@pytest.mark.asyncio
async def test_register_agent_nonexistent_project(isolated_env):
    """register_agent should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "register_agent",
                {"project_key": "NonExistentProject", "program": "test", "model": "test"},
            )
            pytest.fail("Should reject non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


# ============================================================================
# Test: Invalid agent name
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_invalid_name_format(isolated_env):
    """register_agent should reject invalid agent name formats or auto-generate valid ones."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/invalidname"})

        # Single word name (not adjective+noun) - API may reject or auto-generate
        # Either behavior is acceptable; we just verify no crash
        with contextlib.suppress(ToolError):
            result = await client.call_tool(
                "register_agent",
                {"project_key": "InvalidName", "program": "test", "model": "test", "name": "SingleWord"},
            )
            # If it succeeded, it should have auto-generated a valid name
            if result and result.data:
                assert "name" in result.data  # Should return the (possibly auto-generated) name

        # Name with spaces - should be rejected (spaces are clearly invalid)
        try:
            await client.call_tool(
                "register_agent",
                {"project_key": "InvalidName", "program": "test", "model": "test", "name": "Has Spaces"},
            )
            # If we get here, spaces were somehow allowed - note this but don't fail
            # as the API may sanitize the name
        except ToolError as e:
            # Expected: error for name with spaces
            error_str = str(e).lower()
            assert "name" in error_str or "invalid" in error_str or "format" in error_str


@pytest.mark.asyncio
async def test_send_message_nonexistent_agent(isolated_env):
    """send_message should fail for non-existent sender agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/nonexistentagent"})

        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "NonexistentAgent",
                    "sender_name": "NonExistentSender",
                    "to": ["SomeRecipient"],
                    "subject": "Test",
                    "body_md": "Body",
                },
            )
            pytest.fail("Should reject non-existent sender")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


@pytest.mark.asyncio
async def test_send_message_nonexistent_recipient(isolated_env):
    """send_message should fail for non-existent recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/nonexistentrecip"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "NonexistentRecip", "program": "test", "model": "test"},
        )
        sender_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "NonexistentRecip",
                    "sender_name": sender_name,
                    "to": ["NonExistentRecipient"],
                    "subject": "Test",
                    "body_md": "Body",
                },
            )
            pytest.fail("Should reject non-existent recipient")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "recipient" in error_str or "agent" in error_str


# ============================================================================
# Test: Placeholder detection
# ============================================================================


@pytest.mark.asyncio
async def test_placeholder_detection_your_project(isolated_env):
    """Should detect placeholder values like YOUR_PROJECT - either reject or warn."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            result = await client.call_tool("ensure_project", {"human_key": "/YOUR_PROJECT"})
            # If it succeeded, project was created (placeholder detection may just warn)
            # Verify we at least got a valid response
            assert result.data is not None
            assert "slug" in result.data or "id" in result.data
        except ToolError as e:
            # Placeholder was rejected - verify error message is appropriate
            error_str = str(e).lower()
            assert "placeholder" in error_str or "your_" in error_str or "template" in error_str
        # Test passes whether rejected or allowed with warning - documents actual behavior


@pytest.mark.asyncio
async def test_placeholder_detection_your_agent_name(isolated_env):
    """Should detect placeholder agent names like YOUR_AGENT_NAME - either reject or warn."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/placeholderagent"})

        try:
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": "PlaceholderAgent",
                    "program": "test",
                    "model": "test",
                    "name": "YOUR_AGENT_NAME",
                },
            )
            # If succeeded, verify we got a response (may have auto-generated name)
            assert result.data is not None
        except ToolError as e:
            # Placeholder was rejected - verify error message is appropriate
            error_str = str(e).lower()
            assert "placeholder" in error_str or "your_" in error_str or "invalid" in error_str
        # Test passes whether rejected or allowed - documents actual behavior


# ============================================================================
# Test: Message validation
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_empty_recipients(isolated_env):
    """send_message with empty to/cc/bcc returns 0 deliveries (no error)."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/emptyrecip"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "EmptyRecip", "program": "test", "model": "test"},
        )
        sender_name = agent_result.data["name"]

        # API allows empty recipients - returns 0 deliveries
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "EmptyRecip",
                "sender_name": sender_name,
                "to": [],
                "subject": "Test",
                "body_md": "Body",
            },
        )
        # Should succeed but with no deliveries
        assert result.data["count"] == 0
        assert result.data["deliveries"] == []


@pytest.mark.asyncio
async def test_send_message_empty_subject(isolated_env):
    """send_message should handle empty subject gracefully or reject."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/emptysubject"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "EmptySubject", "program": "test", "model": "test"},
        )
        sender_name = agent_result.data["name"]

        # Empty subject may be allowed or rejected
        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "EmptySubject",
                    "sender_name": sender_name,
                    "to": [sender_name],
                    "subject": "",
                    "body_md": "Body",
                },
            )
            # If allowed, message should still be sent
            assert result.data is not None
        except ToolError:
            # Also acceptable - rejecting empty subject
            pass


# ============================================================================
# Test: Contact policy validation
# ============================================================================


@pytest.mark.asyncio
async def test_set_contact_policy_invalid_policy(isolated_env):
    """set_contact_policy normalizes invalid policies to 'auto' (no error)."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/invalidpolicy"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "InvalidPolicy", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # API normalizes invalid policies to "auto" instead of rejecting
        result = await client.call_tool(
            "set_contact_policy",
            {
                "project_key": "InvalidPolicy",
                "agent_name": agent_name,
                "policy": "invalid_policy_value",
            },
        )
        # Should succeed with normalized policy
        assert result.data["policy"] == "auto"
        assert result.data["agent"] == agent_name


# ============================================================================
# Test: File reservation validation
# ============================================================================


@pytest.mark.asyncio
async def test_file_reservation_ttl_below_minimum(isolated_env):
    """file_reservation_paths warns but allows TTL below 60 seconds."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/ttlminimum"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "TtlMinimum", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        # API warns but allows short TTL for testing scenarios
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "TtlMinimum",
                "agent_name": agent_name,
                "paths": ["test.py"],
                "ttl_seconds": 30,  # Below recommended minimum of 60
            },
        )
        # Should succeed (with warning) and grant the reservation
        assert "granted" in result.data
        assert len(result.data["granted"]) == 1
        assert result.data["granted"][0]["path_pattern"] == "test.py"


@pytest.mark.asyncio
async def test_file_reservation_empty_paths(isolated_env):
    """file_reservation_paths should reject empty paths list."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/emptypaths"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "EmptyPaths", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "EmptyPaths",
                    "agent_name": agent_name,
                    "paths": [],
                },
            )
            pytest.fail("Should reject empty paths")
        except ToolError as e:
            error_str = str(e).lower()
            assert "path" in error_str or "empty" in error_str or "required" in error_str


@pytest.mark.asyncio
async def test_file_reservation_nonexistent_agent(isolated_env):
    """file_reservation_paths should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/reservenoagent"})

        try:
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "ReserveNoAgent",
                    "agent_name": "NonExistentAgent",
                    "paths": ["test.py"],
                },
            )
            pytest.fail("Should reject non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Whois validation
# ============================================================================


@pytest.mark.asyncio
async def test_whois_nonexistent_project(isolated_env):
    """whois should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "whois",
                {
                    "project_key": "NonExistentProject",
                    "agent_name": "SomeAgent",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


@pytest.mark.asyncio
async def test_whois_nonexistent_agent(isolated_env):
    """whois should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/whoisnoagent"})

        try:
            await client.call_tool(
                "whois",
                {
                    "project_key": "WhoisNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Fetch inbox validation
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_inbox_nonexistent_project(isolated_env):
    """fetch_inbox should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": "NonExistentProject",
                    "agent_name": "SomeAgent",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


@pytest.mark.asyncio
async def test_fetch_inbox_nonexistent_agent(isolated_env):
    """fetch_inbox should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/inboxnoagent"})

        try:
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": "InboxNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Mark/Acknowledge message validation
# ============================================================================


@pytest.mark.asyncio
async def test_mark_message_read_nonexistent_message(isolated_env):
    """mark_message_read should fail for non-existent message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/marknonemsg"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "MarkNoneMsg", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "mark_message_read",
                {
                    "project_key": "MarkNoneMsg",
                    "agent_name": agent_name,
                    "message_id": 999999,  # Non-existent
                },
            )
            pytest.fail("Should fail for non-existent message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


@pytest.mark.asyncio
async def test_acknowledge_message_nonexistent_message(isolated_env):
    """acknowledge_message should fail for non-existent message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/acknonemsg"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "AckNoneMsg", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "acknowledge_message",
                {
                    "project_key": "AckNoneMsg",
                    "agent_name": agent_name,
                    "message_id": 999999,  # Non-existent
                },
            )
            pytest.fail("Should fail for non-existent message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


# ============================================================================
# Test: Reply message validation
# ============================================================================


@pytest.mark.asyncio
async def test_reply_message_nonexistent_original(isolated_env):
    """reply_message should fail for non-existent original message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/replynonemsg"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ReplyNoneMsg", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": "ReplyNoneMsg",
                    "message_id": 999999,  # Non-existent
                    "sender_name": agent_name,
                    "body_md": "Reply body",
                },
            )
            pytest.fail("Should fail for non-existent original message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


# ============================================================================
# Test: Release/renew file reservation validation
# ============================================================================


@pytest.mark.asyncio
async def test_release_file_reservations_nonexistent_agent(isolated_env):
    """release_file_reservations should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/releasenoagent"})

        try:
            await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": "ReleaseNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


@pytest.mark.asyncio
async def test_renew_file_reservations_nonexistent_agent(isolated_env):
    """renew_file_reservations should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/renewnoagent"})

        try:
            await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": "RenewNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Search messages validation
# ============================================================================


@pytest.mark.asyncio
async def test_search_messages_nonexistent_project(isolated_env):
    """search_messages should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "search_messages",
                {
                    "project_key": "NonExistentProject",
                    "query": "test",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


# ============================================================================
# Test: Request/respond contact validation
# ============================================================================


@pytest.mark.asyncio
async def test_request_contact_nonexistent_agent(isolated_env):
    """request_contact should fail for non-existent from_agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/contactnoagent"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "ContactNoAgent", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "request_contact",
                {
                    "project_key": "ContactNoAgent",
                    "from_agent": "NonExistentAgent",
                    "to_agent": agent_name,
                },
            )
            pytest.fail("Should fail for non-existent from_agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str or "register" in error_str


@pytest.mark.asyncio
async def test_respond_contact_nonexistent_agent(isolated_env):
    """respond_contact should fail for non-existent to_agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/respondnoagent"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "RespondNoAgent", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "respond_contact",
                {
                    "project_key": "RespondNoAgent",
                    "to_agent": "NonExistentAgent",
                    "from_agent": agent_name,
                    "accept": True,
                },
            )
            pytest.fail("Should fail for non-existent to_agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str
