from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_core_resources(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        msg = await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "R1", "body_md": "b"},
        )
        payload = (msg.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = payload.get("id") or 1
        # config
        cfg = await client.read_resource("resource://config/environment")
        assert cfg
        # projects
        projs = await client.read_resource("resource://projects")
        assert projs
        # project specific
        proj = await client.read_resource("resource://project/backend")
        assert proj
        # message
        mres = await client.read_resource(f"resource://message/{mid}?project=Backend")
        assert mres
        # inbox
        ires = await client.read_resource("resource://inbox/BlueLake?project=Backend&limit=5")
        assert ires


