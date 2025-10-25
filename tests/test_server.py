import contextlib
from pathlib import Path

import pytest
from fastmcp import Client
from git import Repo
from PIL import Image
from sqlalchemy import text

from mcp_agent_mail.app import (
    build_mcp_server,
    get_project_sibling_data,
    refresh_project_sibling_suggestions,
    update_project_sibling_status,
)
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import get_session


@pytest.mark.asyncio
async def test_messaging_flow(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        health = await client.call_tool("health_check", {})
        assert health.data["status"] == "ok"
        assert health.data["environment"] == "test"

        project = await client.call_tool("ensure_project", {"human_key": "Backend"})
        assert project.data["slug"] == "backend"

        agent = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
                "task_description": "testing",
            },
        )
        assert agent.data["name"] == "BlueLake"

        message = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Test",
                "body_md": "hello",
            },
        )
        # New response shape: deliveries list
        deliveries = message.data.get("deliveries") or []
        assert isinstance(deliveries, list)
        assert deliveries and deliveries[0]["payload"]["subject"] == "Test"

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
            },
        )
        inbox_items = inbox.structured_content.get("result")
        assert isinstance(inbox_items, list)
        assert len(inbox_items) == 1
        assert inbox_items[0]["subject"] == "Test"

        resource_blocks = await client.read_resource("resource://project/backend")
        assert resource_blocks
        text_payload = resource_blocks[0].text
        assert "BlueLake" in text_payload

        storage_root = Path(get_settings().storage.root).resolve()
        profile = storage_root / "projects" / "backend" / "agents" / "BlueLake" / "profile.json"
        assert profile.exists()
        message_file = next(iter((storage_root / "projects" / "backend" / "messages").rglob("*.md")))
        assert "Test" in message_file.read_text()
        repo = Repo(str(storage_root))
        assert repo.head.commit.message.startswith("mail: BlueLake")


@pytest.mark.asyncio
async def test_claim_conflicts_and_release(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "Alpha",
            },
        )
        await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "Beta",
            },
        )

        result = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert result.data["granted"][0]["path_pattern"] == "src/app.py"

        conflict = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
                "paths": ["src/app.py"],
            },
        )
        assert conflict.data["conflicts"]

        release = await client.call_tool(
            "release_claims",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/app.py"],
            },
        )
        assert release.data["released"] == 1

        claims_resource = await client.read_resource("resource://claims/backend")
        assert "src/app.py" in claims_resource[0].text


@pytest.mark.asyncio
async def test_claim_enforcement_blocks_message_on_overlap(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Alpha",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Beta",
            },
        )

        # Beta claims Alpha's inbox surface exclusively (overlap by pattern)
        claim = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
                "paths": ["agents/Alpha/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert claim.data["granted"]

        # Alpha tries to send a message to Alpha (self), which writes to agents/Alpha/inbox/YYYY/MM/...
        # Expect CLAIM_CONFLICT error payload
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Alpha"],
                "subject": "Blocked",
                "body_md": "hello",
            },
        )
        # Client surfaces tool errors via structured_content when error JSON is raised
        sc = resp.structured_content
        # Depending on client wrapper, this may be in error or result; be flexible
        payload = sc.get("error") or sc.get("result") or {}
        # If result was returned, it must include error shape; otherwise, use data if available
        if not payload and hasattr(resp, "data"):
            payload = getattr(resp, "data", {})
        # Ensure error type and conflicts present
        assert isinstance(payload, dict)
        assert payload.get("type") == "CLAIM_CONFLICT" or payload.get("error", {}).get("type") == "CLAIM_CONFLICT"
        conflicts = payload.get("conflicts") or payload.get("error", {}).get("conflicts")
        assert conflicts and isinstance(conflicts, list)


@pytest.mark.asyncio
async def test_search_and_summarize(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "- TODO: implement FTS\n- ACTION: review claims",
            },
        )
        search = await client.call_tool(
            "search_messages",
            {"project_key": "Backend", "query": "FTS", "limit": 5},
        )
        def _get_subject(x):
            if isinstance(x, dict):
                return x.get("subject")
            return getattr(x, "subject", None)
        assert sum(1 for _ in search.data) >= 1

        summary = await client.call_tool(
            "summarize_thread",
            {"project_key": "Backend", "thread_id": "1", "include_examples": True},
        )
        summary_data = summary.data["summary"]
        assert "TODO" in " ".join(summary_data["key_points"])
        assert summary.data["examples"]


@pytest.mark.asyncio
async def test_attachment_conversion(isolated_env):
    storage = Path(get_settings().storage.root).resolve()
    image_path = storage.parent / "temp.png"
    image = Image.new("RGB", (2, 2), color=(255, 0, 0))
    image.save(image_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Artist",
            },
        )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Artist",
                "to": ["Artist"],
                "subject": "Image",
                "body_md": "Here is an image ![pic](%s)" % image_path,
                "attachment_paths": [str(image_path)],
            },
        )
        attachments = (result.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments")
        assert attachments
        storage_root = storage / "projects" / "backend"
        attachment_files = list((storage_root / "attachments").rglob("*.webp"))
        assert attachment_files
    image_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_rich_logger_does_not_throw(isolated_env, monkeypatch):
    # Enable rich logging flags
    from mcp_agent_mail import config as _config
    monkeypatch.setenv("LOG_RICH_ENABLED", "true")
    monkeypatch.setenv("LOG_INCLUDE_TRACE", "true")
    # Rebuild settings cache
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    # Start a client and hit a couple of endpoints to produce logs
    async with Client(server) as client:
        res = await client.call_tool("health_check", {})
        assert res.data["status"] == "ok"
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Logger",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Logger",
                "to": ["Logger"],
                "subject": "Rich",
                "body_md": "hello",
            },
        )


@pytest.mark.asyncio
async def test_server_level_attachment_policy_override(isolated_env, monkeypatch):
    # Force server to convert images regardless of agent policy
    monkeypatch.setenv("CONVERT_IMAGES", "true")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    storage = Path(get_settings().storage.root).resolve()
    image_path = storage.parent / "temp2.png"
    image = Image.new("RGB", (2, 2), color=(0, 255, 0))
    image.save(image_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Photographer",
                # leave attachments_policy default (auto)
            },
        )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Photographer",
                "to": ["Photographer"],
                "subject": "ServerOverride",
                "body_md": "Here ![pic](%s)" % image_path,
                "attachment_paths": [str(image_path)],
                # Do not set convert_images; rely on server default
            },
        )
        attachments = (result.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments")
        assert attachments and any(att.get("type") in {"file", "inline"} for att in attachments)
    image_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_claim_conflict_ttl_transition_allows_after_expiry(isolated_env, monkeypatch):
    # Ensure enforcement is enabled
    monkeypatch.setenv("FILE_RESERVATIONS_ENFORCEMENT_ENABLED", "true")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Alpha",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "Beta",
            },
        )
        # Beta claims Alpha inbox surface, short TTL
        claim = await client.call_tool(
            "reserve_file_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
                "paths": ["agents/Alpha/inbox/*/*/*.md"],
                "ttl_seconds": 1,
                "exclusive": True,
            },
        )
        assert claim.data["granted"]

        # Immediately blocked
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Alpha"],
                "subject": "BlockedNow",
                "body_md": "hello",
            },
        )
        payload = resp.structured_content.get("error") or resp.structured_content.get("result") or {}
        if not payload and hasattr(resp, "data"):
            payload = getattr(resp, "data", {})
        assert isinstance(payload, dict)
        assert payload.get("type") == "CLAIM_CONFLICT" or payload.get("error", {}).get("type") == "CLAIM_CONFLICT"

        # Wait for TTL to expire and retry
        import asyncio as _asyncio
        await _asyncio.sleep(1.2)
        resp2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["Alpha"],
                "subject": "AllowedAfterTTL",
                "body_md": "hello",
            },
        )
        deliveries = resp2.data.get("deliveries") or []
        assert deliveries and deliveries[0]["payload"]["subject"] == "AllowedAfterTTL"


@pytest.mark.asyncio
async def test_project_sibling_suggestions_backend(isolated_env, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/data/projects/smartedgar_mcp"})
        await client.call_tool("ensure_project", {"human_key": "/data/projects/smartedgar_mcp_frontend"})

    await refresh_project_sibling_suggestions(max_pairs=5)
    data = await get_project_sibling_data()

    async with get_session() as session:
        rows = await session.execute(text("SELECT id FROM projects ORDER BY slug"))
        project_ids = [int(row[0]) for row in rows.fetchall()]

    assert len(project_ids) == 2
    first_id, second_id = project_ids
    assert first_id in data and second_id in data
    assert any(entry["peer"]["id"] == second_id for entry in data[first_id]["suggested"])

    confirmation = await update_project_sibling_status(first_id, second_id, "confirmed")
    assert confirmation["status"] == "confirmed"

    updated_map = await get_project_sibling_data()
    assert any(entry["peer"]["id"] == second_id for entry in updated_map[first_id]["confirmed"])
    assert not any(entry["peer"]["id"] == second_id for entry in updated_map[first_id]["suggested"])
    assert any(entry["peer"]["id"] == first_id for entry in updated_map[second_id]["confirmed"])
