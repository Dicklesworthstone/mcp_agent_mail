from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_empty_inbox_and_pagination(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"})

        # Empty inbox
        inbox = await client.call_tool("fetch_inbox", {"project_key": "Backend", "agent_name": "BlueLake", "limit": 5})
        assert isinstance(inbox.data, list) and len(inbox.data) == 0

        # Create 25 messages
        for i in range(25):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": f"S{i}",
                    "body_md": "x",
                },
            )

        # Pagination: fetch first 10
        first = await client.call_tool("fetch_inbox", {"project_key": "Backend", "agent_name": "BlueLake", "limit": 10})
        items = list(first.data)
        assert len(items) == 10
        # Fetch next 10 using since_ts of last message in first page
        def _get(field: str, obj):
            if isinstance(obj, dict):
                return obj.get(field)
            return getattr(obj, field, None)
        last_created = _get("created_ts", items[-1])
        assert last_created is not None
        next_page = await client.call_tool(
            "fetch_inbox",
            {"project_key": "Backend", "agent_name": "BlueLake", "limit": 10, "since_ts": last_created},
        )
        next_items = list(next_page.data)
        # Strictly newer than the 10th-newest message (items[-1]) yields the 9 newer messages.
        assert len(next_items) == 9
        assert all(_get("created_ts", m) > last_created for m in next_items)

        # Thread resource with multiple messages
        # Use the last message id as thread seed and ensure at least 2
        last_msg_id = _get("id", items[0])
        if last_msg_id is not None:
            blocks = await client.read_resource(f"resource://thread/{last_msg_id}?project=Backend&include_bodies=false")
            assert blocks and "messages" in (blocks[0].text or "")


