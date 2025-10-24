from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_core_resources(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Blue"},
        )
        msg = await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "Blue", "to": ["Blue"], "subject": "R1", "body_md": "b"},
        )
        mid = (msg.data.get("deliveries") or [{}])[0].get("payload", {}).get("id") or 1
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
        mres = await client.read_resource(f"resource://message/{mid}?project=backend")
        assert mres
        # thread (seed with numeric id and correct lowercase project)
        tres = await client.read_resource(f"resource://thread/{mid}?project=backend&include_bodies=true")
        assert tres
        # inbox
        ires = await client.read_resource("resource://inbox/Blue?project=backend&limit=5")
        assert ires


