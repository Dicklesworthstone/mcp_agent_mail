from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_tooling_directory_and_metrics_populate(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "Alpha", "to": ["Alpha"], "subject": "Ping", "body_md": "x"},
        )
        # Directory
        blocks = await client.read_resource("resource://tooling/directory")
        assert blocks
        body = blocks[0].text or ""
        assert "messaging" in body or "claims" in body
        # Metrics
        blocks2 = await client.read_resource("resource://tooling/metrics")
        assert blocks2 and "tools" in (blocks2[0].text or "")


@pytest.mark.asyncio
async def test_tooling_recent_filters(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "Alpha"},
        )
        await client.call_tool(
            "health_check",
            {},
        )
        blocks = await client.read_resource("resource://tooling/recent/60?agent=Alpha&project=Backend")
        assert blocks and blocks[0].text
        import json as _json
        data = _json.loads(blocks[0].text)
        assert isinstance(data, dict)
        assert data.get("project") is None or data.get("project") == "Backend" or data.get("entries") is not None
        assert isinstance(data.get("count"), int)
        entries = data.get("entries") or []
        assert isinstance(entries, list)
        for e in entries:
            assert "tool" in e and isinstance(e["tool"], str)
            if e.get("agent") is not None:
                assert e["agent"] == "Alpha"


@pytest.mark.asyncio
async def test_tooling_locks_resource(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    storage_root = Path(settings.storage.root).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".archive.lock"
    lock_path.touch()
    metadata_path = storage_root / ".archive.lock.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": time.time() - 500}), encoding="utf-8")

    async with Client(server) as client:
        blocks = await client.read_resource("resource://tooling/locks")
        assert blocks
        payload = json.loads(blocks[0].text or "{}")
        assert payload.get("summary", {}).get("total") == 1
        assert any(item.get("path") == str(lock_path) for item in payload.get("locks", []))
