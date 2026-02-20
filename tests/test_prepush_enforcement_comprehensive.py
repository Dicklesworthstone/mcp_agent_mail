"""Comprehensive tests for pre-push guard enforcement.

Tests cover all edge cases and scenarios for the pre-push hook:
- Gate/mode checks (enabled/disabled, block/warn/bypass)
- AGENT_NAME requirement
- Conflict detection with various reservation types
- Expired reservation handling
- Own reservation handling
- Non-exclusive reservation handling
- Multiple commit scenarios

Bead: mcp_agent_mail-8hr (Guards: Pre-push Enforcement)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from mcp_agent_mail.guard import render_prepush_script
from mcp_agent_mail.storage import ProjectArchive


class _DummyArchive:
    """Minimal archive mock with just a root path."""

    def __init__(self, root: Path) -> None:
        self.root = root


def _git(cwd: Path, *args: str) -> str:
    """Run a git command and return stdout."""
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip()


def _init_repo_with_remote(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """Create a bare remote and a local repo pointing to it."""
    remote = tmp_path / f"{name}-remote.git"
    _git(tmp_path, "init", "--bare", str(remote))

    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "branch", "-M", "main")

    # Create initial commit so diff-tree works for subsequent commits
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", ".gitkeep")
    _git(repo, "commit", "-m", "initial")

    return repo, remote


def _create_commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Create a file, add it, commit, and return the SHA."""
    filepath = repo / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _future_iso(seconds: int = 600) -> str:
    """Return an ISO timestamp in the future."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 600) -> str:
    """Return an ISO timestamp in the past."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _write_reservation(
    archive_root: Path,
    *,
    agent: str,
    pattern: str,
    exclusive: bool = True,
    expires_ts: str | None = None,
    filename: str = "lock.json",
) -> Path:
    """Write a file reservation JSON to the archive."""
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "agent": agent,
        "exclusive": exclusive,
        "path_pattern": pattern,
    }
    if expires_ts:
        data["expires_ts"] = expires_ts

    path = fr_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run_prepush_hook(
    repo: Path,
    archive_root: Path,
    *,
    stdin_payload: str,
    agent_name: str | None = "TestAgent",
    worktrees_enabled: bool = True,
    guard_mode: str = "block",
    bypass: bool = False,
) -> subprocess.CompletedProcess:
    """Run the pre-push hook script with the given configuration."""
    hook = repo / "pre-push-test.py"
    script = render_prepush_script(cast(ProjectArchive, _DummyArchive(archive_root)))
    hook.write_text(script, encoding="utf-8")

    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1" if worktrees_enabled else "0"
    env["AGENT_MAIL_GUARD_MODE"] = guard_mode
    if bypass:
        env["AGENT_MAIL_BYPASS"] = "1"
    if agent_name:
        env["AGENT_NAME"] = agent_name
    else:
        env.pop("AGENT_NAME", None)

    return subprocess.run(
        [sys.executable, str(hook), "origin"],
        cwd=str(repo),
        env=env,
        input=stdin_payload,
        text=True,
        capture_output=True,
    )


def _make_stdin_payload(repo: Path, local_sha: str) -> str:
    """Create a standard pre-push stdin payload."""
    return f"refs/heads/main {local_sha} refs/heads/main {'0' * 40}\n"


# =============================================================================
# Gate and Mode Tests
# =============================================================================


class TestPrepushGateAndMode:
    """Tests for gate (WORKTREES_ENABLED) and mode (block/warn/bypass) handling."""

    def test_prepush_gate_disabled_exits_zero(self, tmp_path: Path):
        """When WORKTREES_ENABLED=0, hook should exit 0 without checking."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/file.txt", "content", "initial")

        archive_root = tmp_path / "archive"
        # Create a reservation that WOULD conflict
        _write_reservation(archive_root, agent="Other", pattern="src/**")

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            worktrees_enabled=False,  # Gate disabled
        )
        assert result.returncode == 0

    def test_prepush_bypass_mode_exits_zero(self, tmp_path: Path):
        """When AGENT_MAIL_BYPASS=1, hook should exit 0 without checking."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/file.txt", "content", "initial")

        archive_root = tmp_path / "archive"
        _write_reservation(archive_root, agent="Other", pattern="src/**")

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            bypass=True,
        )
        assert result.returncode == 0
        assert "bypass" in result.stderr.lower()

    def test_prepush_warn_mode_prints_but_exits_zero(self, tmp_path: Path):
        """In warn mode, conflicts are printed but hook exits 0."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/file.txt", "content", "initial")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root, agent="Other", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            guard_mode="warn",
        )
        assert result.returncode == 0
        assert "conflict" in result.stderr.lower()

    def test_prepush_advisory_mode_same_as_warn(self, tmp_path: Path):
        """Advisory mode should behave like warn mode."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/file.txt", "content", "initial")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root, agent="Other", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            guard_mode="advisory",
        )
        assert result.returncode == 0


# =============================================================================
# AGENT_NAME Requirement Tests
# =============================================================================


class TestPrepushAgentNameRequirement:
    """Tests for AGENT_NAME environment variable requirement."""

    def test_prepush_no_agent_name_exits_one(self, tmp_path: Path):
        """Without AGENT_NAME, hook should exit 1."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/file.txt", "content", "initial")
        archive_root = tmp_path / "archive"

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            agent_name=None,  # No AGENT_NAME
        )
        assert result.returncode == 1
        assert "AGENT_NAME" in result.stderr


# =============================================================================
# File Reservation Conflict Tests
# =============================================================================


class TestPrepushConflictDetection:
    """Tests for file reservation conflict detection."""

    def test_prepush_blocks_on_exclusive_conflict(self, tmp_path: Path):
        """Exclusive reservation conflict should block push."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root, agent="OtherAgent", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            agent_name="TestAgent",
        )
        assert result.returncode == 1
        assert "conflict" in result.stderr.lower()
        assert "OtherAgent" in result.stderr

    def test_prepush_no_conflict_passes(self, tmp_path: Path):
        """No conflict should allow push."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "docs/readme.md", "# Readme", "add docs")

        archive_root = tmp_path / "archive"
        # Reservation is for src/**, not docs/**
        _write_reservation(
            archive_root, agent="OtherAgent", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0

    def test_prepush_no_reservations_passes(self, tmp_path: Path):
        """No reservations at all should allow push."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        # No reservations created

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0

    def test_prepush_reservations_dir_missing_passes(self, tmp_path: Path):
        """Missing file_reservations directory should allow push."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "nonexistent-archive"
        # archive_root doesn't even exist

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0


# =============================================================================
# Own Reservation Tests
# =============================================================================


class TestPrepushOwnReservation:
    """Tests for handling of agent's own reservations."""

    def test_prepush_own_reservation_not_conflict(self, tmp_path: Path):
        """Agent's own reservation should not cause conflict."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="TestAgent",  # Same as AGENT_NAME
            pattern="src/**",
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
            agent_name="TestAgent",
        )
        assert result.returncode == 0


# =============================================================================
# Expired Reservation Tests
# =============================================================================


class TestPrepushExpiredReservation:
    """Tests for handling of expired reservations."""

    def test_prepush_expired_reservation_not_conflict(self, tmp_path: Path):
        """Expired reservation should not cause conflict."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="src/**",
            expires_ts=_past_iso(),  # Expired
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0

    def test_prepush_no_expiry_still_conflicts(self, tmp_path: Path):
        """Reservation without expiry should still cause conflict."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="src/**",
            expires_ts=None,  # No expiry
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1


# =============================================================================
# Non-Exclusive Reservation Tests
# =============================================================================


class TestPrepushNonExclusiveReservation:
    """Tests for handling of non-exclusive (shared) reservations."""

    def test_prepush_nonexclusive_reservation_not_conflict(self, tmp_path: Path):
        """Non-exclusive (shared) reservation should not cause conflict."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="src/**",
            exclusive=False,  # Shared
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0


# =============================================================================
# Multiple Commit Tests
# =============================================================================


class TestPrepushMultipleCommits:
    """Tests for pre-push with multiple commits."""

    def test_prepush_multiple_commits_any_conflict_blocks(self, tmp_path: Path):
        """Multiple commits where one conflicts should block."""
        repo, _ = _init_repo_with_remote(tmp_path)

        # First commit - safe
        _create_commit(repo, "docs/readme.md", "# Readme", "add docs")
        # Second commit - conflicts
        sha = _create_commit(repo, "src/main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root, agent="OtherAgent", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1

    def test_prepush_multiple_commits_none_conflict_passes(self, tmp_path: Path):
        """Multiple commits with no conflicts should pass."""
        repo, _ = _init_repo_with_remote(tmp_path)

        _create_commit(repo, "docs/readme.md", "# Readme", "add docs")
        sha = _create_commit(repo, "tests/test_main.py", "# test", "add tests")

        archive_root = tmp_path / "archive"
        # Reservation for src/** shouldn't conflict with docs/ or tests/
        _write_reservation(
            archive_root, agent="OtherAgent", pattern="src/**", expires_ts=_future_iso()
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 0


# =============================================================================
# Pattern Matching Tests
# =============================================================================


class TestPrepushPatternMatching:
    """Tests for glob pattern matching in conflict detection."""

    def test_prepush_exact_file_match(self, tmp_path: Path):
        """Exact file pattern should match."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "config.yaml", "key: value", "add config")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="config.yaml",  # Exact match
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1

    def test_prepush_wildcard_extension_match(self, tmp_path: Path):
        """Wildcard extension pattern should match."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "main.py", "print('hello')", "add main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="*.py",
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1

    def test_prepush_recursive_glob_match(self, tmp_path: Path):
        """Recursive glob pattern should match nested files."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "src/deep/nested/file.py", "# code", "add nested")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="src/**",
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1

    def test_prepush_rename_conflicts_with_old_path(self, tmp_path: Path):
        """Renames should include old + new paths for conflict checks."""
        repo, _ = _init_repo_with_remote(tmp_path)
        _create_commit(repo, "src/old_name.txt", "hello", "add old name")
        _git(repo, "mv", "src/old_name.txt", "src/new_name.txt")
        _git(repo, "commit", "-m", "rename file")
        sha = _git(repo, "rev-parse", "HEAD")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root,
            agent="OtherAgent",
            pattern="src/old_name.txt",
            expires_ts=_future_iso(),
        )

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        assert result.returncode == 1


# =============================================================================
# Empty Push Tests
# =============================================================================


class TestPrepushEmptyPush:
    """Tests for pre-push with no changes."""

    def test_prepush_empty_stdin_passes(self, tmp_path: Path):
        """Empty stdin (no refs to push) should pass."""
        repo, _ = _init_repo_with_remote(tmp_path)
        _create_commit(repo, "file.txt", "content", "initial")
        archive_root = tmp_path / "archive"

        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload="",  # Empty
        )
        assert result.returncode == 0

    def test_prepush_no_new_commits_passes(self, tmp_path: Path):
        """Push with no new commits should pass."""
        repo, _ = _init_repo_with_remote(tmp_path)
        sha = _create_commit(repo, "file.txt", "content", "initial")

        # Push to remote first
        _git(repo, "push", "-u", "origin", "main")

        archive_root = tmp_path / "archive"
        _write_reservation(
            archive_root, agent="OtherAgent", pattern="**", expires_ts=_future_iso()
        )

        # Now try to push again with no new commits
        result = _run_prepush_hook(
            repo,
            archive_root,
            stdin_payload=_make_stdin_payload(repo, sha),
        )
        # Should pass because there are no new commits to check
        assert result.returncode == 0
