from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_claim_overlap_conflict_path(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "Alpha"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "Beta"})
        res1 = await client.call_tool("reserve_file_paths", {"project_key": "Backend", "agent_name": "Alpha", "paths": ["src/**"], "exclusive": True, "ttl_seconds": 3600})
        assert res1.data["granted"]
        res2 = await client.call_tool("reserve_file_paths", {"project_key": "Backend", "agent_name": "Beta", "paths": ["src/app.py"], "exclusive": True, "ttl_seconds": 3600})
        # Advisory model: still granted but conflicts populated
        assert res2.data["granted"] and res2.data["conflicts"]


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome_failure_nonfatal(isolated_env, monkeypatch):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "Req"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "Tgt"})
        result = await client.call_tool(
            "macro_contact_handshake",
            {"project_key": "Backend", "requester": "Req", "target": "Tgt", "auto_accept": True, "welcome_subject": "Hi", "welcome_body": "Welcome"},
        )
        assert "request" in result.data and "response" in result.data

