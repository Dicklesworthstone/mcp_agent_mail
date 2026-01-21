"""Tests for git index.lock retry logic in storage.py.

Tests cover:
- _is_git_index_lock_error() detection function
- _try_clean_stale_git_lock() cleanup function
- GitIndexLockError exception class
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_agent_mail.storage import (
    GitIndexLockError,
    _is_git_index_lock_error,
    _try_clean_stale_git_lock,
)

# ============================================================================
# _is_git_index_lock_error() Tests
# ============================================================================


class TestIsGitIndexLockError:
    """Tests for the _is_git_index_lock_error detection function."""

    def test_detects_file_exists_error_errno_17(self):
        """FileExistsError with errno 17 is a git index lock error."""
        exc = FileExistsError(17, "File exists", ".git/index.lock")
        exc.errno = 17
        assert _is_git_index_lock_error(exc) is True

    def test_ignores_file_exists_error_other_errno(self):
        """FileExistsError with other errno is not detected."""
        exc = FileExistsError(2, "No such file", "somefile")
        exc.errno = 2
        assert _is_git_index_lock_error(exc) is False

    def test_detects_oserror_with_index_lock_message(self):
        """OSError with 'index.lock' in message is detected."""
        exc = OSError("Could not acquire lock: .git/index.lock")
        assert _is_git_index_lock_error(exc) is True

    def test_detects_oserror_with_lock_at_message(self):
        """OSError with 'lock at' in message is detected."""
        exc = OSError("Unable to create lock at .git/index.lock")
        assert _is_git_index_lock_error(exc) is True

    def test_detects_oserror_with_cause_chain(self):
        """OSError with index lock error in __cause__ is detected."""
        cause = FileExistsError(17, "File exists", ".git/index.lock")
        cause.errno = 17
        exc = OSError("Git operation failed")
        exc.__cause__ = cause
        assert _is_git_index_lock_error(exc) is True

    def test_ignores_unrelated_oserror(self):
        """Unrelated OSError is not detected."""
        exc = OSError("Permission denied")
        assert _is_git_index_lock_error(exc) is False

    def test_ignores_non_oserror_exceptions(self):
        """Non-OSError exceptions are not detected."""
        assert _is_git_index_lock_error(ValueError("test")) is False
        assert _is_git_index_lock_error(RuntimeError("test")) is False
        assert _is_git_index_lock_error(Exception("test")) is False

    def test_detects_case_insensitive_message(self):
        """Detection is case-insensitive."""
        exc = OSError("Could not acquire INDEX.LOCK")
        assert _is_git_index_lock_error(exc) is True


# ============================================================================
# _try_clean_stale_git_lock() Tests
# ============================================================================


class TestTryCleanStaleGitLock:
    """Tests for the _try_clean_stale_git_lock cleanup function."""

    def test_returns_false_if_lock_not_exists(self, tmp_path):
        """Returns False when lock file doesn't exist."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        result = _try_clean_stale_git_lock(repo_root)
        assert result is False

    def test_returns_false_if_lock_is_fresh(self, tmp_path):
        """Returns False when lock file is newer than max_age."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        lock_path = repo_root / ".git" / "index.lock"
        lock_path.touch()

        # Lock is brand new, should not be removed
        result = _try_clean_stale_git_lock(repo_root, max_age_seconds=300.0)
        assert result is False
        assert lock_path.exists()

    def test_removes_stale_lock(self, tmp_path):
        """Removes lock file older than max_age."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        lock_path = repo_root / ".git" / "index.lock"
        lock_path.touch()

        # Backdate the lock file by modifying mtime
        old_time = time.time() - 400  # 400 seconds old
        import os
        os.utime(lock_path, (old_time, old_time))

        result = _try_clean_stale_git_lock(repo_root, max_age_seconds=300.0)
        assert result is True
        assert not lock_path.exists()

    def test_handles_missing_git_directory(self, tmp_path):
        """Handles case where .git directory doesn't exist."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # No .git directory

        result = _try_clean_stale_git_lock(repo_root)
        assert result is False

    def test_handles_permission_error_gracefully(self, tmp_path):
        """Handles permission errors when checking lock file."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        lock_path = repo_root / ".git" / "index.lock"
        lock_path.touch()

        # Mock stat to raise PermissionError
        with patch.object(Path, "stat", side_effect=PermissionError("No access")):
            result = _try_clean_stale_git_lock(repo_root)
            # Should return False without raising
            assert result is False


# ============================================================================
# GitIndexLockError Tests
# ============================================================================


class TestGitIndexLockError:
    """Tests for the GitIndexLockError exception class."""

    def test_stores_lock_path(self, tmp_path):
        """Exception stores lock_path attribute."""
        lock_path = tmp_path / ".git" / "index.lock"
        exc = GitIndexLockError("test message", lock_path=lock_path, attempts=3)

        assert exc.lock_path == lock_path
        assert str(exc) == "test message"

    def test_stores_attempts_count(self, tmp_path):
        """Exception stores attempts count."""
        lock_path = tmp_path / ".git" / "index.lock"
        exc = GitIndexLockError("test message", lock_path=lock_path, attempts=5)

        assert exc.attempts == 5

    def test_inherits_from_exception(self, tmp_path):
        """GitIndexLockError is a proper Exception subclass."""
        lock_path = tmp_path / ".git" / "index.lock"
        exc = GitIndexLockError("test message", lock_path=lock_path, attempts=1)

        assert isinstance(exc, Exception)

    def test_can_be_raised_and_caught(self, tmp_path):
        """GitIndexLockError can be raised and caught properly."""
        lock_path = tmp_path / ".git" / "index.lock"

        with pytest.raises(GitIndexLockError) as exc_info:
            raise GitIndexLockError(
                "Git index.lock contention after 5 retries",
                lock_path=lock_path,
                attempts=5
            )

        assert exc_info.value.attempts == 5
        assert exc_info.value.lock_path == lock_path
        assert "5 retries" in str(exc_info.value)


# ============================================================================
# Integration-style tests for _commit() retry logic
# ============================================================================


class TestCommitRetryIntegration:
    """Integration tests for commit retry logic that don't require heavy mocking."""

    @pytest.mark.asyncio
    async def test_commit_with_empty_paths_is_noop(self, isolated_env):
        """Committing empty path list returns immediately without errors."""
        from mcp_agent_mail.config import get_settings
        from mcp_agent_mail.storage import _commit, ensure_archive_root

        settings = get_settings()
        _repo_root, repo = await ensure_archive_root(settings)

        # Get initial commit count
        initial_commits = len(list(repo.iter_commits()))

        # Commit with empty paths should be a no-op
        await _commit(repo, settings, "empty commit", [])

        # No new commits should be made
        final_commits = len(list(repo.iter_commits()))
        assert final_commits == initial_commits

    @pytest.mark.asyncio
    async def test_basic_commit_succeeds(self, isolated_env):
        """Basic commit with valid file succeeds."""
        from mcp_agent_mail.config import get_settings
        from mcp_agent_mail.storage import _commit, ensure_archive_root

        settings = get_settings()
        _repo_root, repo = await ensure_archive_root(settings)

        # Create a test file
        working_tree = repo.working_tree_dir
        assert working_tree is not None
        test_file = Path(working_tree) / "test.txt"
        test_file.write_text("test content")

        initial_commits = len(list(repo.iter_commits()))

        # Commit should succeed
        await _commit(repo, settings, "test commit", ["test.txt"])

        # A new commit should exist
        final_commits = len(list(repo.iter_commits()))
        assert final_commits == initial_commits + 1

    @pytest.mark.asyncio
    async def test_commit_message_includes_trailers(self, isolated_env):
        """Commit message includes agent trailers when present."""
        from mcp_agent_mail.config import get_settings
        from mcp_agent_mail.storage import _commit, ensure_archive_root

        settings = get_settings()
        _repo_root, repo = await ensure_archive_root(settings)

        # Create a test file
        working_tree = repo.working_tree_dir
        assert working_tree is not None
        test_file = Path(working_tree) / "trailer_test.txt"
        test_file.write_text("trailer test")

        # Commit with a mail-style message that should trigger trailer extraction
        await _commit(repo, settings, "mail: TestAgent -> OtherAgent | Subject", ["trailer_test.txt"])

        # Check the commit message
        latest_commit = next(iter(repo.iter_commits()))
        assert "TestAgent" in latest_commit.message
