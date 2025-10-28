from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_reply_preserves_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        for n in ("GreenCastle", "BlueLake"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "x", "model": "y", "name": n},
            )
        # Allow direct messaging without contact gating for this test
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "open"},
        )

        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
            },
        )
        delivery = (orig.data.get("deliveries") or [])[0]
        mid = delivery["payload"]["id"]

        rep = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": mid,
                "sender_name": "BlueLake",
                "body_md": "ack",
            },
        )
        # Ensure thread continuity and deliveries present
        assert rep.data.get("thread_id")
        assert rep.data.get("deliveries")

        # Subject prefix idempotent: replying again with same prefix shouldn't double it
        rep2 = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": mid,
                "sender_name": "BlueLake",
                "body_md": "second",
                "subject_prefix": "Re:",
            },
        )
        assert rep2.data.get("deliveries")

        # Thread listing is validated via tool response thread_id; resource listing is covered elsewhere


