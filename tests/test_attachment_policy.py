from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_attachment_policy_override_inline(isolated_env, tmp_path: Path, monkeypatch):
    # Ensure images are small enough to inline
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1048576")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        # Register agent with explicit inline policy
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake", "attachments_policy": "inline"},
        )
        # Create a tiny inline image as data URI in body
        body = "Here is an image ![pic](data:image/webp;base64,AAECAwQ=)"
        res = await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "Inline", "body_md": body},
        )
        data = res.data
        assert any(att.get("type") == "inline" for att in data.get("attachments", []))


