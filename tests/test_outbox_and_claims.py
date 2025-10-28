from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings


@pytest.mark.asyncio
async def test_outbox_resource_lists_sent_messages(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "send_message",
            {"project_key": "Backend", "sender_name": "BlueLake", "to": ["BlueLake"], "subject": "OutboxTest", "body_md": "b"},
        )
        # Use mailbox resource to verify sent message visibility for the agent
        blocks = await client.read_resource("resource://mailbox/BlueLake?project=Backend&limit=10")
        assert blocks and "OutboxTest" in (blocks[0].text or "")


@pytest.mark.asyncio
async def test_renew_file_reservations_extends_expiry_and_updates_artifact(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        # Create a short TTL file reservation
        res = await client.call_tool(
            "file_reservation_paths",
            {"project_key": "Backend", "agent_name": "GreenCastle", "paths": ["docs/*.md"], "ttl_seconds": 2, "exclusive": True},
        )
        reservation = (res.data.get("granted") or [])[0]
        before = reservation.get("expires_ts")
        assert before

        # Sleep briefly to ensure timestamp change
        await asyncio.sleep(0.6)

        # Renew by +60 seconds
        ren = await client.call_tool(
            "renew_file_reservations",
            {"project_key": "Backend", "agent_name": "GreenCastle", "extend_seconds": 60, "paths": ["docs/*.md"]},
        )
        assert ren.data.get("renewed", 0) >= 1
        renewals = ren.data.get("file_reservations") or []
        renewed = renewals[0]
        after = renewed.get("new_expires_ts")
        assert isinstance(after, str) and after > before

        # Also confirm JSON artifact on disk reflects updated expires_ts
        # The artifact is stored by sha1(path_pattern).json under file_reservations/
        import hashlib
        import json
        from pathlib import Path

        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve() / "projects" / "backend" / "file_reservations"
        digest = hashlib.sha1("docs/*.md".encode("utf-8")).hexdigest()
        artifact = storage_root / f"{digest}.json"
        data = json.loads(artifact.read_text(encoding="utf-8"))
        # Compare datetimes as strings
        assert isinstance(data.get("expires_ts"), str)
        # New expiry should be >= renewed["expires_ts"] parsed
        def _parse(ts: str) -> datetime:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)

        assert _parse(data["expires_ts"]) >= _parse(after)


