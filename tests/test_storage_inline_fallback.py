from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_inline_data_uri_attachments_reflected_when_no_conversion(isolated_env, monkeypatch):
    # Force conversion off to exercise inline fallback path
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"},
        )
        body = "Inline ![p](data:image/webp;base64,AAECAwQ=) only"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "InlineOnly",
                "body_md": body,
                "convert_images": False,
            },
        )
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        assert any(att.get("type") == "inline" for att in attachments)


