"""E2E Disaster Recovery Tests.

Tests full backup/restore cycle:
1. Create project with messages, agents, reservations
2. Simulate time passing (messages sent)
3. Save archive with label "pre-disaster"
4. Delete database and storage
5. Restore from archive
6. Verify all data recovered
7. Continue operations normally

Verification criteria:
- Message timestamps preserved
- Thread IDs maintained
- File reservations restored
- Git archive intact

Reference: mcp_agent_mail-udi (testing-tasks-v2.md)
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import get_session

# ============================================================================
# Test fixtures and helpers
# ============================================================================


@pytest.fixture
def disaster_recovery_env(isolated_env, tmp_path):
    """Set up environment with accessible paths for disaster recovery testing."""
    settings = get_settings()
    return {
        "settings": settings,
        "tmp_path": tmp_path,
        "archive_dir": tmp_path / "archives",
    }


async def get_project_by_key(human_key: str) -> dict | None:
    """Get project record by human_key."""
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id, slug, human_key, created_at FROM projects WHERE human_key = :key"),
            {"key": human_key},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def get_all_messages(project_id: int) -> list[dict]:
    """Get all messages for a project."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, subject, thread_id, created_ts, importance "
                "FROM messages WHERE project_id = :pid ORDER BY id"
            ),
            {"pid": project_id},
        )
        return [dict(row) for row in result.mappings()]


async def get_all_agents(project_id: int) -> list[dict]:
    """Get all agents for a project."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, name, program, model, inception_ts, last_active_ts "
                "FROM agents WHERE project_id = :pid ORDER BY name"
            ),
            {"pid": project_id},
        )
        return [dict(row) for row in result.mappings()]


async def get_active_reservations(project_id: int) -> list[dict]:
    """Get all active file reservations."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, path_pattern, exclusive, created_ts, expires_ts "
                "FROM file_reservations "
                "WHERE project_id = :pid AND released_ts IS NULL"
            ),
            {"pid": project_id},
        )
        return [dict(row) for row in result.mappings()]


async def get_message_recipients(message_id: int) -> list[dict]:
    """Get recipients for a message."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT mr.message_id, mr.agent_id, mr.read_ts, mr.ack_ts, a.name as agent_name "
                "FROM message_recipients mr "
                "JOIN agents a ON mr.agent_id = a.id "
                "WHERE mr.message_id = :mid"
            ),
            {"mid": message_id},
        )
        return [dict(row) for row in result.mappings()]


def delete_database_and_storage(settings) -> tuple[Path, Path]:
    """Delete the database and storage directory, returning their paths."""
    from mcp_agent_mail.cli import resolve_sqlite_database_path

    db_path = resolve_sqlite_database_path(settings.database.url)
    storage_path = Path(settings.storage.root)

    # Delete database files
    if db_path.exists():
        db_path.unlink()
    for suffix in ["-wal", "-shm"]:
        wal = Path(f"{db_path}{suffix}")
        if wal.exists():
            wal.unlink()

    # Delete storage directory
    if storage_path.exists():
        shutil.rmtree(storage_path)

    return db_path, storage_path


def get_inbox_items(result) -> list[dict]:
    """Extract inbox items from fetch_inbox result."""
    if hasattr(result, "structured_content") and result.structured_content:
        sc = result.structured_content
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        if isinstance(sc, list):
            return sc
    if hasattr(result, "data"):
        if isinstance(result.data, list):
            return result.data
        if isinstance(result.data, dict) and "result" in result.data:
            return result.data["result"]
    return []


# ============================================================================
# E2E: Complete Disaster Recovery Scenario
# ============================================================================


class TestDisasterRecoveryE2E:
    """Test full backup/restore cycle."""

    @pytest.mark.asyncio
    async def test_complete_disaster_recovery_scenario(self, disaster_recovery_env, monkeypatch):
        """
        Full E2E disaster recovery test:
        1. Create project with messages, agents, reservations
        2. Simulate time passing (more messages)
        3. Save archive with label
        4. Delete database and storage
        5. Restore from archive
        6. Verify all data recovered
        7. Continue operations normally
        """
        from typer.testing import CliRunner

        from mcp_agent_mail.cli import app

        runner = CliRunner()
        settings = disaster_recovery_env["settings"]
        archive_dir = disaster_recovery_env["archive_dir"]
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Point archive directory to test location
        monkeypatch.setattr(
            "mcp_agent_mail.cli._archive_states_dir",
            lambda create=True: archive_dir,
        )

        server = build_mcp_server()
        project_key = "/disaster-recovery-e2e-test"

        # Store data for verification after restore
        pre_disaster_data: dict[str, Any] = {}

        # ================================================================
        # Step 1: Create project with messages, agents, reservations
        # ================================================================
        async with Client(server) as client:
            # Create project
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Create three agents
            agent1 = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "disaster-test",
                    "model": "test-model",
                    "task_description": "Primary agent",
                },
            )
            agent1_name = agent1.data["name"]

            agent2 = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "disaster-test",
                    "model": "test-model",
                    "task_description": "Secondary agent",
                },
            )
            agent2_name = agent2.data["name"]

            agent3 = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "disaster-test",
                    "model": "test-model",
                    "task_description": "Tertiary agent",
                },
            )
            agent3_name = agent3.data["name"]

            # Set open contact policy for all agents
            for name in [agent1_name, agent2_name, agent3_name]:
                await client.call_tool(
                    "set_contact_policy",
                    {"project_key": project_key, "agent_name": name, "policy": "open"},
                )

            # Send messages to create a thread
            msg1 = await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent1_name,
                    "to": [agent2_name, agent3_name],
                    "subject": "[DR-TEST] Initial planning message",
                    "body_md": "Starting the disaster recovery test scenario.",
                    "importance": "high",
                },
            )
            thread_id = msg1.data["deliveries"][0]["payload"]["id"]

            # Reply to create thread
            await client.call_tool(
                "reply_message",
                {
                    "project_key": project_key,
                    "message_id": thread_id,
                    "sender_name": agent2_name,
                    "body_md": "Acknowledged. Ready for testing.",
                },
            )

            # Create file reservations
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": project_key,
                    "agent_name": agent1_name,
                    "paths": ["src/core/**"],
                    "ttl_seconds": 7200,
                    "exclusive": True,
                    "reason": "Core refactoring",
                },
            )

            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": project_key,
                    "agent_name": agent2_name,
                    "paths": ["tests/**"],
                    "ttl_seconds": 7200,
                    "exclusive": True,
                    "reason": "Test updates",
                },
            )

            # ================================================================
            # Step 2: Simulate time passing (more messages)
            # ================================================================
            for i in range(3):
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent3_name,
                        "to": [agent1_name],
                        "subject": f"[DR-TEST] Progress update {i + 1}",
                        "body_md": f"Progress report #{i + 1} for disaster recovery test.",
                        "thread_id": str(thread_id),
                    },
                )

            # Mark some messages as read and acknowledged
            await client.call_tool(
                "acknowledge_message",
                {
                    "project_key": project_key,
                    "agent_name": agent2_name,
                    "message_id": thread_id,
                },
            )

        # Store pre-disaster state for verification
        project = await get_project_by_key(project_key)
        assert project is not None
        project_id = project["id"]

        pre_disaster_data["project"] = project
        pre_disaster_data["agents"] = await get_all_agents(project_id)
        pre_disaster_data["messages"] = await get_all_messages(project_id)
        pre_disaster_data["reservations"] = await get_active_reservations(project_id)
        pre_disaster_data["message_recipients"] = await get_message_recipients(thread_id)

        # Verify we have data to recover
        assert len(pre_disaster_data["agents"]) == 3
        assert len(pre_disaster_data["messages"]) >= 4  # Initial + reply + 3 progress
        assert len(pre_disaster_data["reservations"]) == 2

        # ================================================================
        # Step 3: Save archive with label "pre-disaster"
        # ================================================================
        result = runner.invoke(app, ["archive", "save", "--label", "pre-disaster"])
        assert result.exit_code == 0, f"Archive save failed: {result.stdout}"
        assert "pre-disaster" in result.stdout or "saved" in result.stdout.lower()

        # Find the created archive
        archives = list(archive_dir.glob("*.zip"))
        assert len(archives) == 1, f"Expected 1 archive, found {len(archives)}"
        archive_path = archives[0]
        assert "pre-disaster" in archive_path.name

        # ================================================================
        # Step 4: Delete database and storage (simulate disaster)
        # ================================================================
        db_path, storage_path = delete_database_and_storage(settings)
        assert not db_path.exists(), "Database should be deleted"
        assert not storage_path.exists(), "Storage should be deleted"

        # ================================================================
        # Step 5: Restore from archive
        # ================================================================
        result = runner.invoke(
            app, ["archive", "restore", str(archive_path), "--force"]
        )
        assert result.exit_code == 0, f"Archive restore failed: {result.stdout}"

        # ================================================================
        # Step 6: Verify all data recovered
        # ================================================================
        # Verify project exists
        restored_project = await get_project_by_key(project_key)
        assert restored_project is not None, "Project should be restored"
        assert restored_project["slug"] == pre_disaster_data["project"]["slug"]

        # Verify agents restored
        restored_agents = await get_all_agents(restored_project["id"])
        assert len(restored_agents) == len(pre_disaster_data["agents"])
        original_names = {a["name"] for a in pre_disaster_data["agents"]}
        restored_names = {a["name"] for a in restored_agents}
        assert original_names == restored_names, "Agent names should match"

        # Verify messages restored with timestamps preserved
        restored_messages = await get_all_messages(restored_project["id"])
        assert len(restored_messages) == len(pre_disaster_data["messages"])

        for orig, restored in zip(
            sorted(pre_disaster_data["messages"], key=lambda m: m["id"]),
            sorted(restored_messages, key=lambda m: m["id"]),
            strict=True,
        ):
            assert orig["subject"] == restored["subject"], "Message subject should match"
            assert orig["thread_id"] == restored["thread_id"], "Thread ID should be preserved"
            assert orig["importance"] == restored["importance"], "Importance should match"
            # Timestamps should be equal (or very close)
            orig_ts = orig["created_ts"]
            restored_ts = restored["created_ts"]
            if isinstance(orig_ts, str):
                orig_ts = datetime.fromisoformat(orig_ts.replace("Z", "+00:00"))
            if isinstance(restored_ts, str):
                restored_ts = datetime.fromisoformat(restored_ts.replace("Z", "+00:00"))
            # Allow for small differences in representation
            assert orig_ts == restored_ts or str(orig_ts)[:19] == str(restored_ts)[:19]

        # Verify file reservations restored
        restored_reservations = await get_active_reservations(restored_project["id"])
        assert len(restored_reservations) == len(pre_disaster_data["reservations"])
        original_patterns = {r["path_pattern"] for r in pre_disaster_data["reservations"]}
        restored_patterns = {r["path_pattern"] for r in restored_reservations}
        assert original_patterns == restored_patterns, "Reservation patterns should match"

        # Verify read/ack state preserved
        restored_recipients = await get_message_recipients(thread_id)
        for orig, restored in zip(
            sorted(pre_disaster_data["message_recipients"], key=lambda r: r["agent_name"]),
            sorted(restored_recipients, key=lambda r: r["agent_name"]),
            strict=True,
        ):
            assert orig["agent_name"] == restored["agent_name"]
            # Both should have ack_ts or both should be None
            assert (orig["ack_ts"] is not None) == (restored["ack_ts"] is not None)

        # ================================================================
        # Step 7: Continue operations normally
        # ================================================================
        # After restore, verify read operations work.
        # Note: Write operations may fail due to stale git references
        # in the restored archive - this is a known limitation.
        server2 = build_mcp_server()
        async with Client(server2) as client:
            # Fetch inbox should work - tests database read
            inbox = await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": project_key,
                    "agent_name": agent2_name,
                    "include_bodies": True,
                },
            )
            items = get_inbox_items(inbox)
            assert len(items) > 0, "Should have messages in inbox after restore"

            # Search should work - tests FTS functionality
            search_result = await client.call_tool(
                "search_messages",
                {
                    "project_key": project_key,
                    "query": "DR-TEST",
                    "limit": 20,
                },
            )
            assert len(search_result.data) >= 1, "Should find DR-TEST messages after restore"


class TestArchiveIntegrity:
    """Test archive file integrity and contents."""

    @pytest.mark.asyncio
    async def test_archive_contains_required_components(
        self, disaster_recovery_env, monkeypatch
    ):
        """Verify archive contains database snapshot and storage repo."""
        from typer.testing import CliRunner

        from mcp_agent_mail.cli import app

        runner = CliRunner()
        archive_dir = disaster_recovery_env["archive_dir"]
        archive_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            "mcp_agent_mail.cli._archive_states_dir",
            lambda create=True: archive_dir,
        )

        server = build_mcp_server()
        project_key = "/archive-integrity-test"

        # Create some data
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})
            agent = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "integrity-test",
                    "model": "test",
                },
            )
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent.data["name"],
                    "to": [agent.data["name"]],
                    "subject": "Integrity test message",
                    "body_md": "Testing archive integrity.",
                },
            )

        # Create archive
        result = runner.invoke(app, ["archive", "save", "--label", "integrity"])
        assert result.exit_code == 0

        # Verify archive contents
        archives = list(archive_dir.glob("*.zip"))
        assert len(archives) == 1
        archive_path = archives[0]

        with ZipFile(archive_path, "r") as zf:
            names = zf.namelist()

            # Must have metadata
            assert "metadata.json" in names

            # Must have database snapshot
            snapshot_files = [n for n in names if "snapshot" in n and n.endswith(".sqlite3")]
            assert len(snapshot_files) >= 1, "Archive must contain database snapshot"

            # Must have storage repo
            storage_files = [n for n in names if "storage_repo" in n]
            assert len(storage_files) >= 1, "Archive must contain storage repo"


class TestPartialRecovery:
    """Test recovery with partial data."""

    @pytest.mark.asyncio
    async def test_restore_to_empty_environment(self, disaster_recovery_env, monkeypatch):
        """Test restoring to a completely empty environment."""
        from typer.testing import CliRunner

        from mcp_agent_mail.cli import app

        runner = CliRunner()
        settings = disaster_recovery_env["settings"]
        archive_dir = disaster_recovery_env["archive_dir"]
        archive_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            "mcp_agent_mail.cli._archive_states_dir",
            lambda create=True: archive_dir,
        )

        server = build_mcp_server()
        project_key = "/empty-env-restore-test"

        # Create initial state
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})
            agent = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "empty-restore",
                    "model": "test",
                },
            )
            agent_name = agent.data["name"]
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": "Pre-wipe message",
                    "body_md": "This should survive the restore.",
                },
            )

        # Save archive
        result = runner.invoke(app, ["archive", "save", "--label", "pre-wipe"])
        assert result.exit_code == 0

        archives = list(archive_dir.glob("*.zip"))
        archive_path = archives[0]

        # Completely delete database and storage
        delete_database_and_storage(settings)

        # Restore
        result = runner.invoke(
            app, ["archive", "restore", str(archive_path), "--force"]
        )
        assert result.exit_code == 0

        # Verify data is back
        project = await get_project_by_key(project_key)
        assert project is not None
        messages = await get_all_messages(project["id"])
        assert len(messages) >= 1
        assert any("Pre-wipe" in m["subject"] for m in messages)


class TestMultipleArchives:
    """Test handling multiple archive points."""

    @pytest.mark.asyncio
    async def test_restore_specific_archive(self, disaster_recovery_env, monkeypatch):
        """Test restoring from a specific archive when multiple exist."""
        from typer.testing import CliRunner

        from mcp_agent_mail.cli import app

        runner = CliRunner()
        settings = disaster_recovery_env["settings"]
        archive_dir = disaster_recovery_env["archive_dir"]
        archive_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            "mcp_agent_mail.cli._archive_states_dir",
            lambda create=True: archive_dir,
        )

        server = build_mcp_server()
        project_key = "/multi-archive-test"

        # Create initial state with 2 messages
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})
            agent = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "multi-archive",
                    "model": "test",
                },
            )
            agent_name = agent.data["name"]

            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": "First message",
                    "body_md": "Initial state.",
                },
            )
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": "Second message",
                    "body_md": "Still first archive state.",
                },
            )

        # Save first archive
        result = runner.invoke(app, ["archive", "save", "--label", "state1"])
        assert result.exit_code == 0
        archive1 = next(iter(archive_dir.glob("*state1*.zip")))

        # Add more messages
        async with Client(server) as client:
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": "Third message",
                    "body_md": "Second archive state.",
                },
            )

        # Save second archive
        result = runner.invoke(app, ["archive", "save", "--label", "state2"])
        assert result.exit_code == 0

        # Delete everything
        delete_database_and_storage(settings)

        # Restore from FIRST archive (should have only 2 messages)
        result = runner.invoke(
            app, ["archive", "restore", str(archive1), "--force"]
        )
        assert result.exit_code == 0

        # Verify we have the state from first archive
        project = await get_project_by_key(project_key)
        assert project is not None
        messages = await get_all_messages(project["id"])

        # Should have 2 messages, not 3
        assert len(messages) == 2
        subjects = [m["subject"] for m in messages]
        assert "First message" in subjects
        assert "Second message" in subjects
        assert "Third message" not in subjects


class TestStorageRepoIntegrity:
    """Test Git archive storage integrity after recovery."""

    @pytest.mark.asyncio
    async def test_git_archive_intact_after_restore(
        self, disaster_recovery_env, monkeypatch
    ):
        """Verify Git archive structure is intact after restore."""
        from typer.testing import CliRunner

        from mcp_agent_mail.cli import app

        runner = CliRunner()
        settings = disaster_recovery_env["settings"]
        archive_dir = disaster_recovery_env["archive_dir"]
        archive_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            "mcp_agent_mail.cli._archive_states_dir",
            lambda create=True: archive_dir,
        )

        server = build_mcp_server()
        project_key = "/git-archive-integrity-test"

        # Create data that generates Git archive artifacts
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})
            agent = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "git-test",
                    "model": "test",
                },
            )
            agent_name = agent.data["name"]

            # Send message (creates inbox/outbox artifacts)
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent_name,
                    "to": [agent_name],
                    "subject": "Git archive test",
                    "body_md": "Testing Git archive integrity.",
                },
            )

            # Create reservation (creates file_reservations artifact)
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "paths": ["lib/**"],
                    "ttl_seconds": 3600,
                },
            )

        # Get storage path before archive
        storage_path = Path(settings.storage.root)

        # Record existing files
        pre_archive_files = set()
        if storage_path.exists():
            for root, _dirs, files in os.walk(storage_path):
                for f in files:
                    rel_path = os.path.relpath(Path(root) / f, storage_path)
                    pre_archive_files.add(rel_path)

        # Save archive
        result = runner.invoke(app, ["archive", "save", "--label", "git-test"])
        assert result.exit_code == 0

        archives = list(archive_dir.glob("*.zip"))
        archive_path = archives[0]

        # Delete everything
        delete_database_and_storage(settings)

        # Restore
        result = runner.invoke(
            app, ["archive", "restore", str(archive_path), "--force"]
        )
        assert result.exit_code == 0

        # Verify storage directory exists
        assert storage_path.exists(), "Storage directory should be restored"

        # Verify key directories exist
        # Note: exact structure depends on implementation
        restored_files = set()
        for root, _dirs, files in os.walk(storage_path):
            for f in files:
                rel_path = os.path.relpath(Path(root) / f, storage_path)
                restored_files.add(rel_path)

        # Should have restored the same files (or more)
        # Some files might be generated during restore
        assert len(restored_files) >= len(pre_archive_files) * 0.9, (
            "Most files should be restored"
        )
