from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_macro_start_session(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": "/backend",
                "program": "codex",
                "model": "gpt-5",
                "task_description": "macro",
                "agent_name": "BlueLake",
                "inbox_limit": 5,
            },
        )
        data = res.data
        assert data["project"]["slug"] == "backend"
        assert data["agent"]["name"] == "BlueLake"
        assert "file_reservations" in data and "inbox" in data


@pytest.mark.asyncio
async def test_macro_prepare_thread(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        m1 = await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "T", "body_md": "b", "thread_id": "TKT-1"},
        )
        _ = m1.data
        prep = await client.call_tool(
            "macro_prepare_thread",
            {
                "project_key": "Backend",
                "thread_id": "TKT-1",
                "program": "codex",
                "model": "gpt-5",
                "agent_name": "BlueLake",
                "include_examples": True,
                "inbox_limit": 5,
            },
        )
        pdata = prep.data
        assert pdata["thread"]["thread_id"] == "TKT-1"
        assert "summary" in pdata["thread"]


@pytest.mark.asyncio
async def test_macro_file_reservation_cycle(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        res = await client.call_tool(
            "macro_file_reservation_cycle",
            {
                "project_key": "Backend",
                "agent_name": "GreenCastle",
                "paths": ["src/*.py"],
                "ttl_seconds": 60,
                "exclusive": True,
            "auto_release": True,
            },
        )
        data = res.data
        assert "file_reservations" in data
    assert data.get("released") is not None


@pytest.mark.asyncio
async def test_renew_file_reservations_extends_expiry(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        g = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "GreenCastle",
                "paths": ["src/app.py"],
                "ttl_seconds": 60,
                "exclusive": True,
            },
        )
        assert g.data["granted"]
        r = await client.call_tool(
            "renew_file_reservations",
            {
                "project_key": "Backend",
                "agent_name": "GreenCastle",
                "paths": ["src/app.py"],
                "extend_seconds": 600,
            },
        )
        assert r.data.get("renewed", 0) >= 1

