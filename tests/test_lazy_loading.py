"""Tests for lazy loading meta-tools."""

from __future__ import annotations

import pytest
from fastmcp import Client, Context

from mcp_agent_mail.app import (
    CORE_TOOLS,
    EXTENDED_TOOLS,
    _EXTENDED_TOOL_REGISTRY,
    build_mcp_server,
)


@pytest.mark.asyncio
async def test_list_extended_tools(isolated_env):
    """Test list_extended_tools returns correct metadata."""
    mcp = build_mcp_server()
    async with Client(mcp) as client:
        result = await client.call_tool("list_extended_tools", arguments={})

        # Check structure
        assert "total" in result
        assert "by_category" in result
        assert "tools" in result

        # Check correct count (19 extended tools)
        assert result["total"] == 19
        assert result["total"] == len(EXTENDED_TOOLS)

        # Check all tools have valid metadata
        assert len(result["tools"]) == 19
        for tool in result["tools"]:
            assert "name" in tool
            assert "category" in tool
            assert "description" in tool
            assert tool["name"] in EXTENDED_TOOLS

        # Check by_category structure
        assert isinstance(result["by_category"], dict)
        # Flatten all categories to verify all tools are included
        all_categorized_tools = []
        for tools_list in result["by_category"].values():
            all_categorized_tools.extend(tools_list)
        assert len(all_categorized_tools) == 19


@pytest.mark.asyncio
async def test_call_extended_tool_valid(isolated_env):
    """Test calling a valid extended tool."""
    mcp = build_mcp_server()
    async with Client(mcp) as client:
        # First setup: create a project and agent for the test
        await client.call_tool("ensure_project", arguments={"human_key": "/tmp/test_project"})
        await client.call_tool("register_agent", arguments={"project_key": "/tmp/test_project", "agent_name": "test_agent"})

        # Test calling an extended tool via the meta-tool
        # Use list_contacts as it's a simple read-only tool
        result = await client.call_tool(
            "call_extended_tool",
            arguments={
                "tool_name": "list_contacts",
                "arguments": {
                    "project_key": "/tmp/test_project",
                    "agent_name": "test_agent"
                }
            }
        )

        # Should succeed and return a list
        assert isinstance(result, list)


@pytest.mark.asyncio
async def test_call_extended_tool_invalid(isolated_env):
    """Test calling invalid tool raises ValueError."""
    mcp = build_mcp_server()
    async with Client(mcp) as client:
        # Try to call a non-existent tool
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "call_extended_tool",
                arguments={
                    "tool_name": "fake_tool_does_not_exist",
                    "arguments": {}
                }
            )

        # Should raise ValueError with helpful message
        assert "Unknown extended tool" in str(exc_info.value)


@pytest.mark.asyncio
async def test_call_extended_tool_invalid_arguments(isolated_env):
    """Test calling with invalid arguments raises ValueError."""
    mcp = build_mcp_server()
    async with Client(mcp) as client:
        # Try to call a valid tool but with wrong arguments
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "call_extended_tool",
                arguments={
                    "tool_name": "list_contacts",
                    "arguments": {
                        "wrong_arg": "value"
                    }
                }
            )

        # Should raise error about invalid arguments
        assert "Invalid arguments" in str(exc_info.value) or "required" in str(exc_info.value).lower()


def test_extended_tool_registry_populated():
    """Test all extended tools are in registry."""
    # Build the server to trigger registry population
    mcp = build_mcp_server()

    # Check registry has correct count
    assert len(_EXTENDED_TOOL_REGISTRY) == len(EXTENDED_TOOLS)

    # Check all extended tools are registered
    for tool_name in EXTENDED_TOOLS:
        assert tool_name in _EXTENDED_TOOL_REGISTRY, f"Tool {tool_name} not in registry"
        assert callable(_EXTENDED_TOOL_REGISTRY[tool_name]), f"Tool {tool_name} is not callable"


def test_core_and_extended_tools_disjoint():
    """Test that core and extended tools don't overlap."""
    overlap = CORE_TOOLS & EXTENDED_TOOLS
    assert len(overlap) == 0, f"Core and extended tools should not overlap, but found: {overlap}"


def test_extended_tools_count():
    """Test that we have exactly 19 extended tools."""
    assert len(EXTENDED_TOOLS) == 19, f"Expected 19 extended tools, but found {len(EXTENDED_TOOLS)}"


def test_core_tools_count():
    """Test that we have exactly 8 core tools."""
    assert len(CORE_TOOLS) == 8, f"Expected 8 core tools, but found {len(CORE_TOOLS)}"
