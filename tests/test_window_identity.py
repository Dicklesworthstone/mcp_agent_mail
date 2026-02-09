"""Tests for the persistent window-based agent identity system (bd-1tz).

Covers:
- Window identity creation on first registration with MCP_AGENT_MAIL_WINDOW_ID
- Window identity reuse on subsequent registrations
- Priority chain: explicit name > window identity > auto-generate
- Window identity lifecycle (list, rename, expire)
- Edge cases: invalid UUID, multiple agents same window, no env var
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import validate_agent_name_format

logger = logging.getLogger(__name__)


# ============================================================================
# Unit Tests: Window Identity Creation and Reuse
# ============================================================================


@pytest.mark.asyncio
async def test_window_id_created_on_first_registration(isolated_env, monkeypatch):
    """A new UUID in MCP_AGENT_MAIL_WINDOW_ID should create a new window identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program",
                "model": "test-model",
            },
        )

        agent_name = result.data["name"]
        assert agent_name is not None
        assert validate_agent_name_format(agent_name)
        # Window identity fields should be present
        assert result.data.get("window_id") == window_uuid
        assert result.data.get("window_display_name") == agent_name
        logger.debug("Window identity created: uuid=%s, name=%s", window_uuid, agent_name)


@pytest.mark.asyncio
async def test_window_id_reused_on_subsequent_registration(isolated_env, monkeypatch):
    """Same UUID should return the same display_name on re-registration."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program",
                "model": "test-model",
            },
        )
        name1 = result1.data["name"]

        # Re-register with same window UUID
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program-v2",
                "model": "test-model-v2",
            },
        )
        name2 = result2.data["name"]

        assert name1 == name2, "Same window UUID should produce same agent name"
        assert result2.data.get("window_id") == window_uuid
        logger.debug("Window identity reused: uuid=%s, name=%s", window_uuid, name1)


@pytest.mark.asyncio
async def test_window_id_without_env_var(isolated_env, monkeypatch):
    """Without MCP_AGENT_MAIL_WINDOW_ID, behavior should be unchanged (no window fields)."""
    monkeypatch.delenv("MCP_AGENT_MAIL_WINDOW_ID", raising=False)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program",
                "model": "test-model",
            },
        )

        assert result.data["name"] is not None
        assert "window_id" not in result.data
        assert "window_display_name" not in result.data


@pytest.mark.asyncio
async def test_window_id_invalid_format(isolated_env, monkeypatch):
    """Non-UUID value for MCP_AGENT_MAIL_WINDOW_ID should fall back to auto-generate."""
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "not-a-valid-uuid")

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program",
                "model": "test-model",
            },
        )

        # Should still work but without window identity
        assert result.data["name"] is not None
        assert validate_agent_name_format(result.data["name"])
        assert "window_id" not in result.data


# ============================================================================
# Priority Chain Tests
# ============================================================================


@pytest.mark.asyncio
async def test_explicit_name_takes_priority_over_window(isolated_env, monkeypatch):
    """Priority 1: Explicit name should override window identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test-program",
                "model": "test-model",
                "name": "GreenCastle",
            },
        )

        assert result.data["name"] == "GreenCastle"
        # Window identity should still be created for tracking
        assert result.data.get("window_id") == window_uuid
        logger.debug(
            "Explicit name priority: name=%s, window_id=%s",
            result.data["name"],
            result.data.get("window_id"),
        )


@pytest.mark.asyncio
async def test_window_display_name_unique_per_project(isolated_env, monkeypatch):
    """Different windows in the same project should get different names."""
    uuid1 = str(uuid.uuid4())
    uuid2 = str(uuid.uuid4())

    from mcp_agent_mail.config import clear_settings_cache

    server = build_mcp_server()

    # First window
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", uuid1)
    clear_settings_cache()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})
        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test",
                "model": "test",
            },
        )

    # Second window
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", uuid2)
    clear_settings_cache()
    async with Client(server) as client:
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test",
                "model": "test",
            },
        )

    assert result1.data["name"] != result2.data["name"]


# ============================================================================
# Window Identity Management Tools
# ============================================================================


@pytest.mark.asyncio
async def test_list_window_identities(isolated_env, monkeypatch):
    """list_window_identities should return active window identities."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test",
                "model": "test",
            },
        )

        result = await client.call_tool(
            "list_window_identities",
            {"project_key": "/test/window"},
        )

        assert result.data["count"] >= 1
        identities = result.data["identities"]
        found = [i for i in identities if i["window_uuid"] == window_uuid]
        assert len(found) == 1
        assert found[0]["display_name"] is not None


@pytest.mark.asyncio
async def test_rename_window(isolated_env, monkeypatch):
    """rename_window should update the display name."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})
        reg = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test",
                "model": "test",
            },
        )
        old_name = reg.data["name"]

        result = await client.call_tool(
            "rename_window",
            {
                "project_key": "/test/window",
                "window_uuid": window_uuid,
                "new_display_name": "SilverFox",
            },
        )

        assert result.data["display_name"] == "SilverFox"
        assert result.data["old_display_name"] == old_name


@pytest.mark.asyncio
async def test_expire_window(isolated_env, monkeypatch):
    """expire_window should mark the identity as expired."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "test",
                "model": "test",
            },
        )

        result = await client.call_tool(
            "expire_window",
            {
                "project_key": "/test/window",
                "window_uuid": window_uuid,
            },
        )

        assert result.data["expired"] is True

        # After expiry, list should not include it
        list_result = await client.call_tool(
            "list_window_identities",
            {"project_key": "/test/window"},
        )
        found = [i for i in list_result.data["identities"] if i["window_uuid"] == window_uuid]
        assert len(found) == 0


# ============================================================================
# Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_multiple_agents_same_window(isolated_env, monkeypatch):
    """Multiple agents registered with the same window UUID should share identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})

        # Register first agent (auto-generated name from window)
        r1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "agent-1",
                "model": "model-1",
            },
        )

        # Register second agent with explicit name but same window
        r2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "agent-2",
                "model": "model-2",
                "name": "BlueLake",
            },
        )

        # Both should reference the same window identity
        assert r1.data.get("window_id") == window_uuid
        assert r2.data.get("window_id") == window_uuid


@pytest.mark.asyncio
async def test_window_persistence_across_sessions(isolated_env, monkeypatch):
    """Window identity should persist across separate client sessions."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()

    # First session
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/window"})
        r1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "session-1",
                "model": "test",
            },
        )
        name1 = r1.data["name"]

    # Second session (new client, same server/DB)
    async with Client(server) as client:
        r2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/window",
                "program": "session-2",
                "model": "test",
            },
        )
        name2 = r2.data["name"]

    assert name1 == name2, "Window identity should persist across sessions"
