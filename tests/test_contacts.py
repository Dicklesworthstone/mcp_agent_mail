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
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "block_all"},
        )

        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
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
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "contacts_only"},
        )

        blocked = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Ping",
                "body_md": "x",
            },
        )
        p1 = _extract_error_payload(blocked)
        assert p1.get("type") == "CONTACT_REQUIRED" or p1.get("error", {}).get("type") == "CONTACT_REQUIRED"

        req = await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "GreenCastle", "to_agent": "BlueLake", "reason": "coordination"},
        )
        assert req.data.get("status") == "pending"

        resp = await client.call_tool(
            "respond_contact",
            {"project_key": "Backend", "to_agent": "BlueLake", "from_agent": "GreenCastle", "accept": True},
        )
        assert resp.data.get("approved") is True

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "AfterApproval",
                "body_md": "y",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert deliveries and deliveries[0]["payload"]["subject"] == "AfterApproval"


@pytest.mark.asyncio
async def test_contact_auto_allows_recent_overlapping_file_reservations(isolated_env):
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

        # Overlapping file reservations -> auto allow contact
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "GreenCastle",
                "paths": ["src/app.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
                "paths": ["src/*.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool("ensure_project", {"human_key": "/frontend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "claude", "model": "opus", "name": "BlueLake"},
        )

        # Request/approve cross-project contact
        req = await client.call_tool(
            "request_contact",
            {"project_key": "Backend", "from_agent": "GreenCastle", "to_agent": "BlueLake", "to_project": "Frontend"},
        )
        assert req.data.get("status") == "pending"

        resp = await client.call_tool(
            "respond_contact",
            {"project_key": "Frontend", "to_agent": "BlueLake", "from_agent": "GreenCastle", "from_project": "Backend", "accept": True},
        )
        assert resp.data.get("approved") is True

        # Now route a message from Backend->Frontend
        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "CrossProject",
                "body_md": "hello",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert any(d.get("project") == "Frontend" for d in deliveries)


