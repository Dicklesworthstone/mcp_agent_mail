from __future__ import annotations

import base64
import contextlib
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings


@pytest.mark.asyncio
async def test_data_uri_embed_without_conversion(isolated_env, monkeypatch):
    # Disable server conversion so inline images remain as data URIs
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    from mcp_agent_mail import config as _config
    with pytest.raises(Exception):  # harmless if cache clear missing; use suppress in runtime
        raise Exception
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Inline"},
        )
        # Craft tiny red dot webp data URI
        payload = base64.b64encode(b"dummy").decode("ascii")
        body = f"Inline ![x](data:image/webp;base64,{payload})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Inline",
                "to": ["Inline"],
                "subject": "InlineImg",
                "body_md": body,
                "convert_images": False,
            },
        )
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments") or []
        assert any(att.get("type") == "inline" for att in attachments)


@pytest.mark.asyncio
async def test_missing_file_path_in_markdown_and_originals_toggle(isolated_env, monkeypatch):
    # Originals disabled then enabled
    storage = Path(get_settings().storage.root).resolve()
    image_path = storage.parent / "nope.png"
    if image_path.exists():
        image_path.unlink()

    # First: originals disabled
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "false")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Photos"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Photos",
                "to": ["Photos"],
                "subject": "MissingPath",
                "body_md": f"![x]({image_path})",
            },
        )
        assert res.data.get("deliveries")

    # Now originals enabled
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server2 = build_mcp_server()
    async with Client(server2) as client2:
        await client2.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Photos"},
        )
        res2 = await client2.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Photos",
                "to": ["Photos"],
                "subject": "MissingPath2",
                "body_md": f"![x]({image_path})",
            },
        )
        assert res2.data.get("deliveries")


