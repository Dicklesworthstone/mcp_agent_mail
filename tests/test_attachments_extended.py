from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastmcp import Client
from PIL import Image

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings


@pytest.mark.asyncio
async def test_attachments_keep_originals_and_manifest(isolated_env, monkeypatch):
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_path = storage_root.parent / "img_o.png"
    img = Image.new("RGB", (4, 4), color=(0, 0, 255))
    img.save(img_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Orig",
                "body_md": "see",
                "attachment_paths": [str(img_path)],
            },
        )
        assert res.data.get("deliveries")
        # Check originals and manifest presence
        proj = storage_root / "projects" / "backend"
        manifests = list((proj / "attachments" / "_manifests").glob("*.json"))
        assert manifests, "expected manifest json"
        originals = list((proj / "attachments" / "originals").rglob("*.*"))
        assert originals, "expected originals stored"
    img_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_attachment_inline_vs_file_threshold(isolated_env, monkeypatch):
    # Large threshold -> inline
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1048576")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_path = storage_root.parent / "img_t.png"
    img = Image.new("RGB", (8, 8), color=(255, 0, 0))
    img.save(img_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Inline expected
        r_inline = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Inline",
                "body_md": "body",
                "attachment_paths": [str(img_path)],
            },
        )
        atts1 = (r_inline.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        assert any(a.get("type") == "inline" for a in atts1)

        # Small threshold -> file
        monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        r_file = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "File",
                "body_md": "body",
                "attachment_paths": [str(img_path)],
            },
        )
        atts2 = (r_file.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        assert any(a.get("type") == "file" for a in atts2)
    img_path.unlink(missing_ok=True)

