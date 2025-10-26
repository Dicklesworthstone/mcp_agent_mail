from __future__ import annotations

import contextlib
import json

import pytest
from fastmcp import Client

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
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        for name in ("Alpha", "Beta"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": name},
            )

        # Beta blocks all
        await client.call_tool(
            "set_contact_policy", {"project_key": "Backend", "agent_name": "Beta", "policy": "block_all"}
        )
        r1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Hi",
                "body_md": "ping",
            },
        )
        payload1 = r1.structured_content.get("error") or r1.structured_content.get("result") or {}
        if not payload1 and hasattr(r1, "data"):
            payload1 = getattr(r1, "data", {})
        assert (payload1.get("type") == "CONTACT_BLOCKED") or (
            payload1.get("error", {}).get("type") == "CONTACT_BLOCKED"
        )

        # Beta requires contacts_only
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "Beta", "policy": "contacts_only"},
        )
        r2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Hi",
                "body_md": "ping",
            },
        )
        payload2 = r2.structured_content.get("error") or r2.structured_content.get("result") or {}
        if not payload2 and hasattr(r2, "data"):
            payload2 = getattr(r2, "data", {})
        assert (payload2.get("type") == "CONTACT_REQUIRED") or (
            payload2.get("error", {}).get("type") == "CONTACT_REQUIRED"
        )

        # Request and approve contact; then messaging should succeed
        await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "Alpha", "to_agent": "Beta", "reason": "work"},
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": "Backend",
                "to_agent": "Beta",
                "from_agent": "Alpha",
                "accept": True,
            },
        )
        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Welcome",
                "body_md": "hello",
            },
        )
        assert ok.data.get("deliveries")


@pytest.mark.asyncio
async def test_contact_auto_allows_claim_overlap(isolated_env, monkeypatch):
    # contacts_only with overlapping claims should auto-allow
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Beta"},
        )
        await client.call_tool(
            "set_contact_policy", {"project_key": "Backend", "agent_name": "Beta", "policy": "contacts_only"}
        )

        # Overlapping claims: Alpha holds src/*, Beta holds src/app.py
        g1 = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/*"],
                "ttl_seconds": 600,
                "exclusive": True,
            },
        )
        assert g1.data["granted"]
        g2 = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
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
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Heuristic",
                "body_md": "claims overlap allows",
            },
        )
        assert ok.data.get("deliveries")


@pytest.mark.asyncio
async def test_cross_project_contact_and_delivery(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool("ensure_project", {"human_key": "Frontend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "Alpha", "to_agent": "project:Frontend#BlueLake"},
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": "Frontend",
                "to_agent": "BlueLake",
                "from_agent": "Alpha",
                "from_project": "Backend",
                "accept": True,
            },
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["project:Frontend#BlueLake"],
                "subject": "XProj",
                "body_md": "hello",
            },
        )
        deliveries = sent.data.get("deliveries") or []
        assert deliveries and any(d.get("project") == "Frontend" for d in deliveries)

        # Verify appears in Frontend inbox
        inbox_blocks = await client.read_resource("resource://inbox/BlueLake?project=Frontend&limit=10")
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert any(item.get("subject") == "XProj" for item in data.get("messages", []))


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Beta"},
        )

        res = await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "Backend",
                "requester": "Alpha",
                "target": "Beta",
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
