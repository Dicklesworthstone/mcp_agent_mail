from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


def _extract_error_payload(resp: Any) -> dict[str, Any]:
    sc = getattr(resp, "structured_content", {}) or {}
    payload = sc.get("error") or sc.get("result") or {}
    if not payload and hasattr(resp, "data"):
        payload = getattr(resp, "data", {})
    return payload if isinstance(payload, dict) else {}


@pytest.mark.asyncio
async def test_contact_policy_block_all_blocks_direct_message(isolated_env):
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
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "Beta", "policy": "block_all"},
        )

        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Hello",
                "body_md": "test",
            },
        )
        payload = _extract_error_payload(resp)
        assert payload.get("type") == "CONTACT_BLOCKED" or payload.get("error", {}).get("type") == "CONTACT_BLOCKED"


@pytest.mark.asyncio
async def test_contacts_only_requires_approval_then_allows(isolated_env):
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
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "Beta", "policy": "contacts_only"},
        )

        blocked = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "Ping",
                "body_md": "x",
            },
        )
        p1 = _extract_error_payload(blocked)
        assert p1.get("type") == "CONTACT_REQUIRED" or p1.get("error", {}).get("type") == "CONTACT_REQUIRED"

        req = await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "Alpha", "to_agent": "Beta", "reason": "coordination"},
        )
        assert req.data.get("status") == "pending"

        resp = await client.call_tool(
            "respond_contact",
            {"project_key": "Backend", "to_agent": "Beta", "from_agent": "Alpha", "accept": True},
        )
        assert resp.data.get("approved") is True

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "AfterApproval",
                "body_md": "y",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert deliveries and deliveries[0]["payload"]["subject"] == "AfterApproval"


@pytest.mark.asyncio
async def test_contact_auto_allows_recent_overlapping_claims(isolated_env):
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

        # Overlapping claims -> auto allow contact
        await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/app.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )
        await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
                "paths": ["src/*.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Beta"],
                "subject": "OverlapOK",
                "body_md": "z",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert deliveries and deliveries[0]["payload"]["subject"] == "OverlapOK"


@pytest.mark.asyncio
async def test_cross_project_contact_handshake_routes_message(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        # Two projects
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool("ensure_project", {"human_key": "Frontend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Green"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "claude", "model": "opus", "name": "Blue"},
        )

        # Request/approve cross-project contact
        req = await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "Green", "to_agent": "Blue", "to_project": "Frontend"},
        )
        assert req.data.get("status") == "pending"

        resp = await client.call_tool(
            "respond_contact",
            {"project_key": "Frontend", "to_agent": "Blue", "from_agent": "Green", "from_project": "Backend", "accept": True},
        )
        assert resp.data.get("approved") is True

        # Now route a message from Backend->Frontend
        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Green",
                "to": ["Blue"],
                "subject": "CrossProject",
                "body_md": "hello",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert any(d.get("project") == "Frontend" for d in deliveries)


