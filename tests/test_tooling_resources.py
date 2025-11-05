from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import slugify


@pytest.mark.asyncio
async def test_tooling_directory_and_metrics_populate(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = Path.cwd().as_posix()
        project_result = await client.call_tool("ensure_project", {"human_key": project_key})
        project_data = project_result.data or {}
        project_slug = project_data.get("slug") or slugify(project_key)

        await client.call_tool(
            "register_agent",
            {"project_key": project_slug, "program": "codex", "model": "gpt-5"},
        )

        project_blocks = await client.read_resource(f"resource://project/{project_slug}")
        assert project_blocks and project_blocks[0].text
        project_payload = json.loads(project_blocks[0].text)
        agents = project_payload.get("agents") or []
        assert agents, "Expected at least one agent after registration"
        agent_name = agents[0]["name"]

        await client.call_tool(
            "send_message",
            {
                "project_key": project_slug,
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Ping",
                "body_md": "x",
            },
        )
        # Directory
        blocks = await client.read_resource("resource://tooling/directory")
        assert blocks
        body = blocks[0].text or ""
        assert "messaging" in body or "file_reservations" in body
        # Metrics
        blocks2 = await client.read_resource("resource://tooling/metrics")
        assert blocks2 and "tools" in (blocks2[0].text or "")


@pytest.mark.asyncio
async def test_tooling_recent_filters(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = Path.cwd().as_posix()
        project_result = await client.call_tool("ensure_project", {"human_key": project_key})
        project_data = project_result.data or {}
        project_slug = project_data.get("slug") or slugify(project_key)

        await client.call_tool(
            "register_agent",
            {"project_key": project_slug, "program": "codex", "model": "gpt-5"},
        )

        project_blocks = await client.read_resource(f"resource://project/{project_slug}")
        assert project_blocks and project_blocks[0].text
        project_payload = json.loads(project_blocks[0].text)
        agents = project_payload.get("agents") or []
        assert agents, "Expected at least one agent after registration"
        agent_name = agents[0]["name"]
        await client.call_tool(
            "health_check",
            {},
        )
        blocks = await client.read_resource(
            f"resource://tooling/recent/60?agent={agent_name}&project={project_slug}"
        )
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
    # Use current process PID and recent timestamp so lock is not considered stale
    # (heal_archive_locks runs at server startup and would remove stale locks)
    metadata_path.write_text(json.dumps({"pid": os.getpid(), "created_ts": time.time()}), encoding="utf-8")

    async with Client(server) as client:
        blocks = await client.read_resource("resource://tooling/locks")
        assert blocks
        payload = json.loads(blocks[0].text or "{}")
        assert payload.get("summary", {}).get("total") == 1
        assert any(item.get("path") == str(lock_path) for item in payload.get("locks", []))
