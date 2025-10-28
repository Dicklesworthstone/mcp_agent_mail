"""Test macro_start_session with file_reservation_paths parameter to prevent regression of the shadowing bug."""

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_macro_start_session_with_file_reservation_paths(isolated_env):
    """
    Test macro_start_session WITH file_reservation_paths parameter.

    This test specifically exercises the code path that was broken by the
    globals().get("file_reservation_paths") bug (now fixed to use mcp.get_tool("file_reservation_paths")).

    The bug was: macro_start_session has a parameter named 'file_reservation_paths' which
    shadowed the file_reservation_paths function. Using mcp.get_tool("file_reservation_paths") avoids global lookups.

    The fix: Use mcp.get_tool("file_reservation_paths") to get the tool from the registry.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": "/test/project",
                "program": "claude-code",
                "model": "sonnet-4.5",
                "agent_name": "BlueLake",  # ← Must be adjective+noun format
                "task_description": "Testing file reservations functionality",
                "file_reservation_paths": ["src/**/*.py", "tests/**/*.py"],
                "file_reservation_reason": "Testing macro_start_session with file reservations",
                "file_reservation_ttl_seconds": 7200,
                "inbox_limit": 10,
            },
        )

        data = res.data

        # Verify project was created
        assert "project" in data
        assert data["project"]["slug"] == "test-project"
        assert data["project"]["human_key"] == "/test/project"

        # Verify agent was registered
        assert "agent" in data
        assert data["agent"]["name"] == "BlueLake"
        assert data["agent"]["program"] == "claude-code"
        assert data["agent"]["model"] == "sonnet-4.5"

        # Verify file reservations were created (this is the critical part!)
        assert "file_reservations" in data
        assert data["file_reservations"] is not None
        assert "granted" in data["file_reservations"]

        # Should have granted reservations for both patterns
        granted_reservations = data["file_reservations"]["granted"]
        assert len(granted_reservations) == 2

        # Verify reservation details
        reservation_paths = {r["path_pattern"] for r in granted_reservations}
        assert "src/**/*.py" in reservation_paths
        assert "tests/**/*.py" in reservation_paths

        for r in granted_reservations:
            assert r["exclusive"] is True
            assert r["reason"] == "Testing macro_start_session with file reservations"
            assert "expires_ts" in r

        # Verify inbox was fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)


@pytest.mark.asyncio
async def test_macro_start_session_without_file_reservations_still_works(isolated_env):
    """Verify that macro_start_session still works when file_reservation_paths is omitted."""
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": "/test/project2",
                "program": "codex",
                "model": "gpt-5",
                "agent_name": "RedStone",  # ← Must be adjective+noun format
                "task_description": "No file reservations test",
                # file_reservation_paths intentionally omitted
                "inbox_limit": 5,
            },
        )

        data = res.data

        # Verify basic functionality still works
        assert data["project"]["slug"] == "test-project2"
        assert data["agent"]["name"] == "RedStone"

        # file_reservations should be empty dict when not requested (not None - function returns {"granted": [], "conflicts": []})
        assert data["file_reservations"] == {"granted": [], "conflicts": []}
        assert len(data["file_reservations"]["granted"]) == 0

        # Inbox should still be fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)
