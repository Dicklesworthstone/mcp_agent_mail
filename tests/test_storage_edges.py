from __future__ import annotations

import base64
import contextlib
import json
import os
import time
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import AsyncFileLock


@pytest.mark.asyncio
async def test_data_uri_embed_without_conversion(isolated_env, monkeypatch):
    # Disable server conversion so inline images remain as data URIs
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    from mcp_agent_mail import config as _config
    # Avoid asserting on a blind Exception type; just test settings cache clear path
    with contextlib.suppress(Exception):
        raise RuntimeError("noop")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Craft tiny red dot webp data URI
        payload = base64.b64encode(b"dummy").decode("ascii")
        body = f"Inline ![x](data:image/webp;base64,{payload})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
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
    storage = Path(get_settings().storage.root).expanduser().resolve()
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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
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
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        res2 = await client2.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
                "subject": "MissingPath2",
                "body_md": f"![x]({image_path})",
            },
        )
        assert res2.data.get("deliveries")


@pytest.mark.asyncio
async def test_async_file_lock_recovers_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    lock_path = tmp_path / ".archive.lock"
    lock_path.touch()
    stale_time = time.time() - 120
    os.utime(lock_path, (stale_time, stale_time))
    metadata_path = tmp_path / f"{lock_path.name}.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": stale_time}))

    lock = AsyncFileLock(lock_path, timeout_seconds=0.1, stale_timeout_seconds=1.0)
    async with lock:
        current = json.loads(metadata_path.read_text())
        assert current.get("pid") == os.getpid()

    # Metadata should be cleaned up after release
    assert not metadata_path.exists()
    assert not lock_path.exists()
