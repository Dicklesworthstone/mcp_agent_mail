import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from git import Repo
from PIL import Image
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from sqlalchemy import text

from mcp_agent_mail.app import (
    build_mcp_server,
    get_project_sibling_data,
    refresh_project_sibling_suggestions,
    update_project_sibling_status,
)
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import get_session


@pytest.mark.asyncio
async def test_messaging_flow(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        health = await client.call_tool("health_check", {})
        assert health.data["status"] == "ok"
        assert health.data["environment"] == "test"

        project = await client.call_tool("ensure_project", {"human_key": "/backend"})
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

        storage_root = Path(get_settings().storage.root).expanduser().resolve()
        profile = storage_root / "projects" / "backend" / "agents" / "BlueLake" / "profile.json"
        assert profile.exists()
        message_file = next(iter((storage_root / "projects" / "backend" / "messages").rglob("*.md")))
        assert "Test" in message_file.read_text()
        repo = Repo(str(storage_root))
        # Commit message is a rich panel; ensure the subject is captured
        assert '"subject": "Test"' in repo.head.commit.message


@pytest.mark.asyncio
async def test_file_reservation_conflicts_and_release(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        alpha_identity = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "GreenHill",
            },
        )
        beta_identity = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "BlueRiver",
            },
        )
        alpha_name = alpha_identity.data["name"]
        beta_name = beta_identity.data["name"]

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": alpha_name,
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert result.data["granted"][0]["path_pattern"] == "src/app.py"

        conflict = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": beta_name,
                "paths": ["src/app.py"],
            },
        )
        # Advisory model: conflicts are reported but reservation is still granted
        assert conflict.data["conflicts"]
        assert conflict.data["granted"]
        assert conflict.data["granted"][0]["path_pattern"] == "src/app.py"

        # Both Alpha and Beta should have active reservations now (advisory model)
        active_only_resource = await client.read_resource("resource://file_reservations/backend?active_only=true")
        active_only_payload = json.loads(active_only_resource[0].text)
        assert len(active_only_payload) == 2
        assert all(entry["path_pattern"] == "src/app.py" for entry in active_only_payload)
        assert {entry["agent"] for entry in active_only_payload} == {alpha_name, beta_name}

        release = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "Backend",
                "agent_name": alpha_name,
                "paths": ["src/app.py"],
            },
        )
        assert release.data["released"] == 1

        file_reservations_resource = await client.read_resource("resource://file_reservations/backend")
        payload = json.loads(file_reservations_resource[0].text)
        assert any(entry["path_pattern"] == "src/app.py" and entry["released_ts"] is not None for entry in payload)

        # After Alpha releases, Beta's reservation should still be active (advisory model)
        active_only_after_release = await client.read_resource("resource://file_reservations/backend?active_only=true")
        active_reservations = json.loads(active_only_after_release[0].text)
        assert len(active_reservations) == 1
        assert active_reservations[0]["agent"] == beta_name
        assert active_reservations[0]["path_pattern"] == "src/app.py"
        assert active_reservations[0]["released_ts"] is None


@pytest.mark.asyncio
async def test_file_reservation_enforcement_blocks_message_on_overlap(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "GreenCastle",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
            },
        )

        # Beta reserves Alpha's inbox surface exclusively (overlap by pattern)
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
                "paths": ["agents/GreenCastle/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        # Alpha tries to send a message to Alpha (self), which writes to agents/Alpha/inbox/YYYY/MM/...
        # Expect FILE_RESERVATION_CONFLICT error payload
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
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
        assert payload.get("type") == "FILE_RESERVATION_CONFLICT" or payload.get("error", {}).get("type") == "FILE_RESERVATION_CONFLICT"
        conflicts = payload.get("conflicts") or payload.get("error", {}).get("conflicts")
        assert conflicts and isinstance(conflicts, list)


@pytest.mark.asyncio
async def test_force_release_file_reservation_stale(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "5")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "1")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": "/backend"})
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
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "GreenLake",
                },
            )
            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "BlueLake",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]

            async with get_session() as session:
                project_row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "backend"})
                project_id = project_row.scalar_one()
                stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
                await session.execute(
                    text(
                        "UPDATE agents SET last_active_ts = :ts WHERE project_id = :pid AND lower(name) = :name"
                    ),
                    {"ts": stale_cutoff, "pid": project_id, "name": "bluelake"},
                )
                await session.commit()

            force = await client.call_tool(
                "force_release_file_reservation",
                {
                    "project_key": "Backend",
                    "agent_name": "GreenLake",
                    "file_reservation_id": reservation_id,
                },
            )
            assert force.data["released"] == 1
            assert force.data["reservation"]["notified"] is True
            resource = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload = json.loads(resource[0].text)
            released = next(item for item in payload if item["id"] == reservation_id)
            assert released["released_ts"] is not None

            inbox = await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": "Backend",
                    "agent_name": "BlueLake",
                },
            )
            messages = inbox.structured_content.get("result", [])
            assert any("Released stale lock" in msg["subject"] for msg in messages)
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_force_release_rejects_recent_activity(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "300")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "120")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": "/backend"})
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
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "GreenLake",
                },
            )
            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "BlueLake",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]
            with pytest.raises(ToolError):
                await client.call_tool(
                    "force_release_file_reservation",
                    {
                        "project_key": "Backend",
                        "agent_name": "GreenLake",
                        "file_reservation_id": reservation_id,
                    },
                )

            resource = await client.read_resource("resource://file_reservations/backend?active_only=true")
            entries = json.loads(resource[0].text)
            assert entries and entries[0]["id"] == reservation_id
            assert entries[0]["released_ts"] is None
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_file_reservation_integration_logging(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "5")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "2")
    clear_settings_cache()

    console = Console(record=True, force_terminal=True)

    def _log(title: str, description: str, data: object | None = None) -> None:
        renderables = [Text(description)]
        if data is not None:
            rendered = json.dumps(data, indent=2, default=str)
            renderables.append(Syntax(rendered, "json", theme="monokai", word_wrap=True))
        console.print(Panel(Group(*renderables), title=title, border_style="cyan"))

    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": "/backend"})
            _log("Project", "Ensured project '/backend'")

            for name in ("BlueLake", "GreenLake"):
                await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "Backend",
                        "program": "codex",
                        "model": "gpt-5",
                        "name": name,
                    },
                )
                _log("Agent Registered", f"Registered agent {name}")

            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "BlueLake",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]
            _log(
                "Reservation Granted",
                "BlueLake reserved src/app.py",
                reservation.data,
            )

            resource_initial = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload_initial = json.loads(resource_initial[0].text)
            _log("Initial Reservations", "Reservation state immediately after grant", payload_initial)

            async with get_session() as session:
                project_row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "backend"})
                project_id = project_row.scalar_one()
                stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
                await session.execute(
                    text("UPDATE agents SET last_active_ts = :ts WHERE project_id = :pid AND lower(name) = :name"),
                    {"ts": stale_cutoff, "pid": project_id, "name": "bluelake"},
                )
                await session.commit()
            _log("Agent Last Active Adjusted", "Artificially aged BlueLake last_active_ts to simulate inactivity.")

            resource_after = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload_after = json.loads(resource_after[0].text)
            _log("Post Sweep", "Reservation state after inactivity sweep", payload_after)

            released_entry = next(item for item in payload_after if item["id"] == reservation_id)
            assert released_entry["released_ts"] is not None
            assert released_entry["stale"] is False
            assert any("agent_inactive" in reason for reason in released_entry["stale_reasons"])

            active_only = await client.read_resource("resource://file_reservations/backend?active_only=true")
            payload_active_only = json.loads(active_only[0].text)
            _log("Active Reservations", "Active-only listing should omit released entries", payload_active_only)
            assert payload_active_only == []

            notebook = console.export_text(clear=False)
            assert "BlueLake reserved" in notebook
            assert "agent_inactive" in notebook
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_search_and_summarize(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
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
                "body_md": "- TODO: implement FTS\n- ACTION: review file reservations",
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
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    image_path = storage_root.parent / "temp.png"
    image = Image.new("RGB", (2, 2), color=(255, 0, 0))
    image.save(image_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "OrangeMountain",
            },
        )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "OrangeMountain",
                "to": ["OrangeMountain"],
                "subject": "Image",
                "body_md": "Here is an image ![pic](%s)" % image_path,
                "attachment_paths": [str(image_path)],
            },
        )
        attachments = (result.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments")
        assert attachments
        project_root = storage_root / "projects" / "backend"
        attachment_files = list((project_root / "attachments").rglob("*.webp"))
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
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "PinkDog",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "PinkDog",
                "to": ["PinkDog"],
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

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    image_path = storage_root.parent / "temp2.png"
    image = Image.new("RGB", (2, 2), color=(0, 255, 0))
    image.save(image_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "WhiteCat",
                # leave attachments_policy default (auto)
            },
        )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "WhiteCat",
                "to": ["WhiteCat"],
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
async def test_file_reservation_conflict_ttl_transition_allows_after_expiry(isolated_env, monkeypatch):
    # Ensure enforcement is enabled
    monkeypatch.setenv("FILE_RESERVATIONS_ENFORCEMENT_ENABLED", "true")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "GreenCastle",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
            },
        )
        # Beta reserves Alpha inbox surface, short TTL
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                    "agent_name": "BlueLake",
                    "paths": ["agents/GreenCastle/inbox/*/*/*.md"],
                "ttl_seconds": 1,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        # Immediately blocked
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
                "subject": "BlockedNow",
                "body_md": "hello",
            },
        )
        payload = resp.structured_content.get("error") or resp.structured_content.get("result") or {}
        if not payload and hasattr(resp, "data"):
            payload = getattr(resp, "data", {})
        assert isinstance(payload, dict)
        assert payload.get("type") == "FILE_RESERVATION_CONFLICT" or payload.get("error", {}).get("type") == "FILE_RESERVATION_CONFLICT"

        # Wait for TTL to expire and retry
        import asyncio as _asyncio
        await _asyncio.sleep(1.2)
        resp2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
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
        await client.call_tool("ensure_project", {"human_key": "/data/projects/backend_core"})
        await client.call_tool("ensure_project", {"human_key": "/data/projects/backend_core_ui"})

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
