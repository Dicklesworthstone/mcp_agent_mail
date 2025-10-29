from __future__ import annotations

import contextlib
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import slugify


@pytest.mark.asyncio
async def test_contact_blocked_and_contacts_only(isolated_env, monkeypatch):
    # Ensure contact enforcement is enabled (it is by default, but be explicit)
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        for name in ("GreenCastle", "BlueLake"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": name},
            )

        # Beta blocks all
        await client.call_tool(
            "set_contact_policy", {"project_key": "Backend", "agent_name": "BlueLake", "policy": "block_all"}
        )
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "GreenCastle",
                    "to": ["BlueLake"],
                    "subject": "Hi",
                    "body_md": "ping",
                },
            )
        assert "Recipient is not accepting messages" in str(excinfo.value)

        # Beta requires contacts_only
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "contacts_only"},
        )
        r2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Hi",
                "body_md": "ping",
            },
        )
        deliveries = r2.data.get("deliveries") or []
        assert deliveries and deliveries[0]["payload"]["subject"] == "Hi"


@pytest.mark.asyncio
async def test_contact_auto_allows_file_reservation_overlap(isolated_env, monkeypatch):
    # contacts_only with overlapping file reservations should auto-allow
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "set_contact_policy", {"project_key": "Backend", "agent_name": "BlueLake", "policy": "contacts_only"}
        )

        # Overlapping file reservations: Alpha holds src/*, Beta holds src/app.py
        g1 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "GreenCastle",
                "paths": ["src/*"],
                "ttl_seconds": 600,
                "exclusive": True,
            },
        )
        assert g1.data["granted"]
        g2 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
                "paths": ["src/app.py"],
                "ttl_seconds": 600,
                "exclusive": True,
            },
        )
        assert g2.data["granted"]

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Heuristic",
                "body_md": "file reservations overlap allows",
            },
        )
        assert ok.data.get("deliveries")


@pytest.mark.asyncio
async def test_cross_project_contact_and_delivery(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool("ensure_project", {"human_key": "/frontend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "GreenCastle", "to_agent": "project:Frontend#BlueLake"},
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": "Frontend",
                "to_agent": "BlueLake",
                "from_agent": "GreenCastle",
                "from_project": "Backend",
                "accept": True,
            },
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["project:Frontend#BlueLake"],
                "subject": "XProj",
                "body_md": "hello",
            },
        )
        deliveries = sent.data.get("deliveries") or []
        assert deliveries and any(d.get("project") in {"Frontend", "/frontend"} for d in deliveries)

        # Verify appears in Frontend inbox
        inbox_blocks = await client.read_resource("resource://inbox/BlueLake?project=Frontend&limit=10")
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert any(item.get("subject") == "XProj" for item in data.get("messages", []))


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        res = await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "Backend",
                "requester": "GreenCastle",
                "target": "BlueLake",
                "reason": "let's sync",
                "auto_accept": True,
                "welcome_subject": "Welcome",
                "welcome_body": "nice to meet you",
            },
        )
        assert res.data.get("request")
        assert res.data.get("response")
        welcome = res.data.get("welcome_message") or {}
        # If the welcome ran, it will have deliveries
        if welcome:
            assert welcome.get("deliveries")


@pytest.mark.asyncio
async def test_macro_contact_handshake_registers_missing_target(isolated_env):
    backend = "/data/projects/backend"
    frontend = "/data/projects/frontend"
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend,
                "requester": "BlueLake",
                "target": "RedDog",
                "to_project": frontend,
                "register_if_missing": True,
                "program": "codex-cli",
                "model": "gpt-5",
                "task_description": "auto-created via handshake",
                "auto_accept": True,
            },
        )

        agents_blocks = await client.read_resource(f"resource://agents/{slugify(frontend)}")
        raw = agents_blocks[0].text if agents_blocks else "{}"
        data = json.loads(raw)
        names = {agent.get("name") for agent in data.get("agents", [])}
        assert "RedDog" in names


@pytest.mark.asyncio
async def test_send_message_supports_at_address(isolated_env):
    backend = "/data/projects/smartedgar_mcp"
    frontend = "/data/projects/smartedgar_mcp_frontend"
    frontend_slug = slugify(frontend)
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": frontend, "program": "codex", "model": "gpt-5", "name": "PinkDog"},
        )

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend,
                "requester": "BlueLake",
                "target": "PinkDog",
                "to_project": frontend,
                "auto_accept": True,
            },
        )

        response = await client.call_tool(
            "send_message",
            {
                "project_key": backend,
                "sender_name": "BlueLake",
                "to": [f"PinkDog@{frontend_slug}"],
                "subject": "AT Route",
                "body_md": "hello",
            },
        )
        deliveries = response.data.get("deliveries") or []
        assert deliveries and any(item.get("project") == frontend for item in deliveries)
