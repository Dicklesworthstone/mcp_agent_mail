"""Tests for virtual namespace file reservations (bd-14z).

Covers:
- Virtual path (tool://, resource://, service://) reservations are created successfully
- Virtual path conflict detection (exact match between different agents)
- Virtual paths don't conflict with filesystem paths
- Virtual paths can be released normally
- Suspicious pattern detection allows virtual paths
- _is_virtual_namespace helper
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import _is_virtual_namespace, build_mcp_server

logger = logging.getLogger(__name__)


def _get_data(result):
    """Extract data dict from tool result."""
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, dict):
            return sc
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return getattr(result, "data", result)


async def _setup_project_with_agents(client, project_key: str, count: int) -> list[str]:
    """Register `count` agents in the project and return their names."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    names = []
    for i in range(count):
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-prog",
                "model": "test-model",
                "task_description": f"agent-{i}",
            },
        )
        data = _get_data(result)
        names.append(data["name"])
    return names


# ============================================================================
# Helper unit tests
# ============================================================================


def test_is_virtual_namespace():
    """_is_virtual_namespace detects virtual path prefixes."""
    assert _is_virtual_namespace("tool://playwright") is True
    assert _is_virtual_namespace("resource://gpu-0") is True
    assert _is_virtual_namespace("service://redis") is True
    assert _is_virtual_namespace("src/main.py") is False
    assert _is_virtual_namespace("**/*.py") is False
    assert _is_virtual_namespace("/absolute/path") is False


# ============================================================================
# Virtual reservation creation
# ============================================================================


@pytest.mark.asyncio
async def test_virtual_reservation_created(isolated_env):
    """Virtual namespace path can be reserved successfully."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-create", 1)

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-create",
                "agent_name": names[0],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": "browser automation",
            },
        )
        data = _get_data(result)
        assert "granted" in data
        granted = data["granted"]
        assert len(granted) == 1
        assert granted[0]["path_pattern"] == "tool://playwright"
        assert granted[0]["exclusive"] is True


@pytest.mark.asyncio
async def test_multiple_virtual_reservations(isolated_env):
    """Multiple virtual namespace paths can be reserved at once."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-multi", 1)

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-multi",
                "agent_name": names[0],
                "paths": ["tool://playwright", "resource://gpu-0", "service://redis"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        data = _get_data(result)
        granted = data["granted"]
        assert len(granted) == 3
        patterns = {g["path_pattern"] for g in granted}
        assert "tool://playwright" in patterns
        assert "resource://gpu-0" in patterns
        assert "service://redis" in patterns


# ============================================================================
# Conflict detection
# ============================================================================


@pytest.mark.asyncio
async def test_virtual_reservation_conflict(isolated_env):
    """Two agents reserving the same virtual path shows a conflict."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-conflict", 2)

        # Agent 0 reserves
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-conflict",
                "agent_name": names[0],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # Agent 1 tries to reserve the same
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-conflict",
                "agent_name": names[1],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        data = _get_data(result)
        # Advisory: still granted, but conflicts reported
        assert len(data["granted"]) == 1
        assert len(data["conflicts"]) >= 1
        conflict = data["conflicts"][0]
        assert conflict["path"] == "tool://playwright"


@pytest.mark.asyncio
async def test_virtual_no_conflict_with_filesystem(isolated_env):
    """Virtual path and filesystem path never conflict, even with similar names."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-no-cross", 2)

        # Agent 0 reserves a filesystem path
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-no-cross",
                "agent_name": names[0],
                "paths": ["src/playwright.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # Agent 1 reserves a virtual path — no conflict expected
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-no-cross",
                "agent_name": names[1],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        data = _get_data(result)
        assert len(data["granted"]) == 1
        assert len(data["conflicts"]) == 0


@pytest.mark.asyncio
async def test_virtual_different_paths_no_conflict(isolated_env):
    """Different virtual paths don't conflict with each other."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-diff", 2)

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-diff",
                "agent_name": names[0],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-diff",
                "agent_name": names[1],
                "paths": ["tool://selenium"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        data = _get_data(result)
        assert len(data["granted"]) == 1
        assert len(data["conflicts"]) == 0


# ============================================================================
# Release
# ============================================================================


@pytest.mark.asyncio
async def test_virtual_reservation_release(isolated_env):
    """Virtual namespace reservations can be released normally."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-release", 1)

        # Reserve
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-release",
                "agent_name": names[0],
                "paths": ["tool://playwright"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # Release
        result = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "/test/vns-release",
                "agent_name": names[0],
                "paths": ["tool://playwright"],
            },
        )
        data = _get_data(result)
        assert data["released"] >= 1


# ============================================================================
# Mixed filesystem + virtual
# ============================================================================


@pytest.mark.asyncio
async def test_mixed_fs_and_virtual_reservation(isolated_env):
    """An agent can hold both filesystem and virtual reservations."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/vns-mixed", 1)

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/vns-mixed",
                "agent_name": names[0],
                "paths": ["src/**/*.py", "tool://playwright", "resource://gpu-0"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        data = _get_data(result)
        assert len(data["granted"]) == 3
        patterns = {g["path_pattern"] for g in data["granted"]}
        assert "tool://playwright" in patterns
        assert "resource://gpu-0" in patterns
