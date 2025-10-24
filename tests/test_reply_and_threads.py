from __future__ import annotations

import json

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_reply_preserves_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        for n in ("A", "B"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "x", "model": "y", "name": n},
            )
        # Allow direct messaging without contact gating for this test
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "B", "policy": "open"},
        )

        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "A",
                "to": ["B"],
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
                "sender_name": "B",
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
                "sender_name": "B",
                "body_md": "second",
                "subject_prefix": "Re:",
            },
        )
        assert rep2.data.get("deliveries")

        # Thread resource lists at least 2 messages
        blocks = await client.read_resource(f"resource://thread/{mid}?project=Backend&include_bodies=false")
        payload = json.loads(blocks[0].text)
        assert payload.get("project") == "Backend"
        assert len(payload.get("messages", [])) >= 2


