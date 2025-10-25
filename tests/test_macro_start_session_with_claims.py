"""Test macro_start_session with reserve_file_paths parameter to prevent regression of the shadowing bug."""

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_macro_start_session_with_reserve_file_paths(isolated_env):
    """
    Test macro_start_session WITH reserve_file_paths parameter.

    This test specifically exercises the code path that was broken by the
    globals().get("reserve_file_paths") bug (now fixed to use mcp.get_tool("reserve_file_paths")).

    The bug was: macro_start_session has a parameter named 'reserve_file_paths' which
    shadowed the reserve_file_paths function. Using globals().get("reserve_file_paths") tried
    to work around this but failed because reserve_file_paths isn't in the global scope.

    The fix: Use mcp.get_tool("reserve_file_paths") to get the tool from the registry.
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
                "task_description": "Testing claims functionality",
                "reserve_file_paths": ["src/**/*.py", "tests/**/*.py"],  # ← This triggers the shadowing
                "claim_reason": "Testing macro_start_session with claims",
                "claim_ttl_seconds": 7200,
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

        # Verify claims were created (this is the critical part!)
        assert "claims" in data
        assert data["claims"] is not None
        assert "granted" in data["claims"]

        # Should have granted claims for both patterns
        granted_claims = data["claims"]["granted"]
        assert len(granted_claims) == 2

        # Verify claim details
        reserve_file_paths = {claim["path_pattern"] for claim in granted_claims}
        assert "src/**/*.py" in reserve_file_paths
        assert "tests/**/*.py" in reserve_file_paths

        for claim in granted_claims:
            assert claim["exclusive"] is True
            assert claim["reason"] == "Testing macro_start_session with claims"
            assert "expires_ts" in claim

        # Verify inbox was fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)


@pytest.mark.asyncio
async def test_macro_start_session_without_claims_still_works(isolated_env):
    """Verify that macro_start_session still works when reserve_file_paths is omitted."""
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": "/test/project2",
                "program": "codex",
                "model": "gpt-5",
                "agent_name": "RedStone",  # ← Must be adjective+noun format
                "task_description": "No claims test",
                # reserve_file_paths intentionally omitted
                "inbox_limit": 5,
            },
        )

        data = res.data

        # Verify basic functionality still works
        assert data["project"]["slug"] == "test-project2"
        assert data["agent"]["name"] == "RedStone"

        # claims should be empty dict when not requested (not None - function returns {"granted": [], "conflicts": []})
        assert data["claims"] == {"granted": [], "conflicts": []}
        assert len(data["claims"]["granted"]) == 0

        # Inbox should still be fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)
