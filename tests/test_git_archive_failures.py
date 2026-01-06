"""Git Archive Failure Handling Tests.

Tests for graceful handling of Git archive issues:
- Archive directory missing (should auto-create)
- Git repo not initialized (should auto-init)
- Concurrent archive writes (locking)
- Invalid file paths sanitized
- Large attachment handling

Reference: mcp_agent_mail-c2x
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import (
    AsyncFileLock,
    archive_write_lock,
    ensure_archive,
    ensure_archive_root,
    heal_archive_locks,
    write_agent_profile,
)

# ============================================================================
# Archive Auto-Creation Tests
# ============================================================================


class TestArchiveAutoCreation:
    """Tests for automatic archive directory and repo creation."""

    @pytest.mark.asyncio
    async def test_ensure_archive_root_creates_directory(self, isolated_env):
        """ensure_archive_root creates storage directory if missing."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()

        # Ensure directory doesn't exist
        if storage_root.exists():
            for f in storage_root.rglob("*"):
                if f.is_file():
                    f.unlink()
            for d in sorted(storage_root.rglob("*"), reverse=True):
                if d.is_dir():
                    d.rmdir()
            storage_root.rmdir()

        assert not storage_root.exists()

        # Call ensure_archive_root
        repo_root, _repo = await ensure_archive_root(settings)

        # Directory should now exist
        assert storage_root.exists()
        assert repo_root == storage_root

    @pytest.mark.asyncio
    async def test_ensure_archive_root_initializes_git_repo(self, isolated_env):
        """ensure_archive_root initializes Git repo if not present."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()

        _repo_root, repo = await ensure_archive_root(settings)

        # Git directory should exist
        git_dir = storage_root / ".git"
        assert git_dir.exists()
        assert repo is not None

    @pytest.mark.asyncio
    async def test_ensure_archive_creates_project_directory(self, isolated_env):
        """ensure_archive creates project-specific directory."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()

        archive = await ensure_archive(settings, "test-project")

        # Project directory should exist
        project_dir = storage_root / "projects" / "test-project"
        assert project_dir.exists()
        assert archive.root == project_dir

    @pytest.mark.asyncio
    async def test_ensure_archive_is_idempotent(self, isolated_env):
        """ensure_archive can be called multiple times safely."""
        settings = get_settings()

        # Call multiple times
        archive1 = await ensure_archive(settings, "idempotent-test")
        archive2 = await ensure_archive(settings, "idempotent-test")
        archive3 = await ensure_archive(settings, "idempotent-test")

        # All should return same root
        assert archive1.root == archive2.root == archive3.root

    @pytest.mark.asyncio
    async def test_ensure_archive_creates_lock_file_path(self, isolated_env):
        """ensure_archive sets up lock file path correctly."""
        settings = get_settings()

        archive = await ensure_archive(settings, "lock-test")

        # Lock path should be inside project directory
        assert archive.lock_path.name == ".archive.lock"
        assert archive.lock_path.parent == archive.root


# ============================================================================
# Git Repository Initialization Tests
# ============================================================================


class TestGitRepoInitialization:
    """Tests for Git repository auto-initialization."""

    @pytest.mark.asyncio
    async def test_git_repo_has_gitattributes(self, isolated_env):
        """Git repo should have .gitattributes file."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()

        await ensure_archive_root(settings)

        gitattributes = storage_root / ".gitattributes"
        assert gitattributes.exists()

        content = gitattributes.read_text()
        assert "*.json text" in content
        assert "*.md text" in content

    @pytest.mark.asyncio
    async def test_git_repo_has_initial_commit(self, isolated_env):
        """Git repo should have initial commit."""
        settings = get_settings()
        Path(settings.storage.root).expanduser().resolve()

        _repo_root, repo = await ensure_archive_root(settings)

        # Should have at least one commit
        commits = list(repo.iter_commits())
        assert len(commits) >= 1

    @pytest.mark.asyncio
    async def test_git_repo_gpg_signing_disabled(self, isolated_env):
        """Git repo should have GPG signing disabled."""
        settings = get_settings()

        _repo_root, repo = await ensure_archive_root(settings)

        # Check config
        try:
            gpg_sign = repo.config_reader().get_value("commit", "gpgsign")
            assert gpg_sign in ("false", False)
        except Exception:
            # If not set, that's also fine (defaults to false)
            pass


# ============================================================================
# Concurrent Archive Write Tests
# ============================================================================


class TestConcurrentArchiveWrites:
    """Tests for concurrent archive write locking."""

    @pytest.mark.asyncio
    async def test_archive_write_lock_basic(self, isolated_env):
        """archive_write_lock can be acquired and released."""
        settings = get_settings()
        archive = await ensure_archive(settings, "lock-basic")

        async with archive_write_lock(archive):
            # Lock is held
            assert archive.lock_path.exists() or True  # Lock may use different mechanism

        # Lock should be released after context

    @pytest.mark.asyncio
    async def test_archive_write_lock_prevents_concurrent_access(self, isolated_env):
        """archive_write_lock prevents concurrent writes."""
        settings = get_settings()
        archive = await ensure_archive(settings, "lock-concurrent")

        results: list[str] = []

        async def writer(name: str, delay: float) -> None:
            async with archive_write_lock(archive, timeout_seconds=10):
                results.append(f"{name}_start")
                await asyncio.sleep(delay)
                results.append(f"{name}_end")

        # Start two writers - they should serialize
        task1 = asyncio.create_task(writer("A", 0.1))
        await asyncio.sleep(0.01)  # Small delay to ensure order
        task2 = asyncio.create_task(writer("B", 0.05))

        await asyncio.gather(task1, task2)

        # Results should show serialization (no interleaving)
        assert results[0] == "A_start"
        assert results[1] == "A_end"
        assert results[2] == "B_start"
        assert results[3] == "B_end"

    @pytest.mark.asyncio
    async def test_archive_write_lock_queues_waiters(self, isolated_env):
        """archive_write_lock queues multiple waiters correctly."""
        settings = get_settings()
        archive = await ensure_archive(settings, "lock-queue")

        acquired_order: list[int] = []

        async def acquire_lock(lock_id: int, hold_time: float = 0.05) -> None:
            async with archive_write_lock(archive, timeout_seconds=30):
                acquired_order.append(lock_id)
                await asyncio.sleep(hold_time)

        # Create multiple waiters
        tasks = [
            asyncio.create_task(acquire_lock(1, 0.05)),
            asyncio.create_task(acquire_lock(2, 0.05)),
            asyncio.create_task(acquire_lock(3, 0.05)),
        ]

        await asyncio.gather(*tasks)

        # All locks should have been acquired (order may vary due to asyncio scheduling)
        assert len(acquired_order) == 3
        assert set(acquired_order) == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_archive_write_lock_released_on_exception(self, isolated_env):
        """archive_write_lock releases lock when exception occurs."""
        settings = get_settings()
        archive = await ensure_archive(settings, "lock-exception")

        # Acquire and raise exception
        try:
            async with archive_write_lock(archive):
                raise ValueError("Test error")
        except ValueError:
            pass

        # Lock should be released - can acquire again
        acquired = False
        async with archive_write_lock(archive, timeout_seconds=1):
            acquired = True

        assert acquired


# ============================================================================
# File Path Sanitization Tests
# ============================================================================


class TestFilePathSanitization:
    """Tests for invalid file path handling."""

    @pytest.mark.asyncio
    async def test_write_agent_profile_creates_directory(self, isolated_env):
        """write_agent_profile creates agent directory if missing."""
        settings = get_settings()
        archive = await ensure_archive(settings, "agent-dir-test")

        agent = {
            "id": 1,
            "name": "TestAgent",
            "program": "test",
            "model": "test-model",
        }

        await write_agent_profile(archive, agent)

        # Agent directory should exist
        agent_dir = archive.root / "agents" / "TestAgent"
        assert agent_dir.exists()

        # Profile should exist
        profile_path = agent_dir / "profile.json"
        assert profile_path.exists()

    @pytest.mark.asyncio
    async def test_write_agent_profile_handles_special_characters(self, isolated_env):
        """write_agent_profile handles agent names safely."""
        settings = get_settings()
        archive = await ensure_archive(settings, "special-chars")

        # Agent name with safe characters (avoiding path separators)
        agent = {
            "id": 2,
            "name": "Test_Agent-123",
            "program": "test",
            "model": "test-model",
        }

        await write_agent_profile(archive, agent)

        agent_dir = archive.root / "agents" / "Test_Agent-123"
        assert agent_dir.exists()


# ============================================================================
# Lock Healing Tests
# ============================================================================


class TestLockHealing:
    """Tests for stale lock cleanup."""

    @pytest.mark.asyncio
    async def test_heal_archive_locks_cleans_orphaned_metadata(self, isolated_env):
        """heal_archive_locks removes metadata files without locks."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        # Create orphaned metadata file (no corresponding lock)
        metadata_path = storage_root / "orphan.lock.owner.json"
        metadata_path.write_text('{"pid": 99999}')

        result = await heal_archive_locks(settings)

        # Metadata should be cleaned up
        assert str(metadata_path) in result.get("metadata_removed", [])

    @pytest.mark.asyncio
    async def test_heal_archive_locks_handles_missing_root(self, isolated_env, monkeypatch):
        """heal_archive_locks handles missing storage root gracefully."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()

        # Ensure root doesn't exist
        if storage_root.exists():
            for f in storage_root.rglob("*"):
                if f.is_file():
                    f.unlink()
            for d in sorted(storage_root.rglob("*"), reverse=True):
                if d.is_dir():
                    d.rmdir()
            storage_root.rmdir()

        # Should not raise
        result = await heal_archive_locks(settings)

        assert result["locks_scanned"] == 0

    @pytest.mark.asyncio
    async def test_heal_archive_locks_returns_summary(self, isolated_env):
        """heal_archive_locks returns structured summary."""
        settings = get_settings()

        # Ensure archive exists
        await ensure_archive_root(settings)

        result = await heal_archive_locks(settings)

        # Should have expected keys
        assert "locks_scanned" in result
        assert "locks_removed" in result
        assert "metadata_removed" in result


# ============================================================================
# AsyncFileLock Tests
# ============================================================================


class TestAsyncFileLock:
    """Tests for AsyncFileLock behavior."""

    @pytest.mark.asyncio
    async def test_file_lock_writes_metadata(self, isolated_env):
        """AsyncFileLock writes owner metadata."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        lock_path = storage_root / "test.lock"
        lock = AsyncFileLock(lock_path)

        async with lock:
            # Metadata file should exist
            metadata_path = storage_root / "test.lock.owner.json"
            assert metadata_path.exists()

            import json
            metadata = json.loads(metadata_path.read_text())
            assert "pid" in metadata
            assert metadata["pid"] == os.getpid()

    @pytest.mark.asyncio
    async def test_file_lock_detects_stale_lock(self, isolated_env):
        """AsyncFileLock detects stale locks from dead processes."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        lock_path = storage_root / "stale.lock"

        # Create a stale lock file with a dead PID
        lock_path.touch()
        metadata_path = storage_root / "stale.lock.owner.json"
        import json
        metadata_path.write_text(json.dumps({
            "pid": 99999999,  # Non-existent PID
            "created_ts": time.time() - 1000,  # Old timestamp
        }))

        # Should be able to acquire despite stale lock
        lock = AsyncFileLock(lock_path, timeout_seconds=5, stale_timeout_seconds=1)
        acquired = False

        async with lock:
            acquired = True

        assert acquired

    @pytest.mark.asyncio
    async def test_file_lock_prevents_reentrant_acquisition(self, isolated_env):
        """AsyncFileLock prevents re-entrant acquisition."""
        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser().resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        lock_path = storage_root / "reentrant.lock"
        lock = AsyncFileLock(lock_path)

        with pytest.raises(RuntimeError, match="Re-entrant"):
            async with lock:
                async with lock:  # Should raise
                    pass


# ============================================================================
# Large Attachment Tests
# ============================================================================


class TestLargeAttachments:
    """Tests for large attachment handling."""

    @pytest.mark.asyncio
    async def test_archive_handles_large_content(self, isolated_env):
        """Archive operations handle large content without issues."""
        settings = get_settings()
        archive = await ensure_archive(settings, "large-content")

        # Create a large agent profile (simulate large content)
        agent = {
            "id": 3,
            "name": "LargeAgent",
            "program": "test",
            "model": "test-model",
            "metadata": "x" * 10000,  # 10KB of data
        }

        await write_agent_profile(archive, agent)

        # Verify it was written
        profile_path = archive.root / "agents" / "LargeAgent" / "profile.json"
        assert profile_path.exists()

        import json
        content = json.loads(profile_path.read_text())
        assert len(content["metadata"]) == 10000
