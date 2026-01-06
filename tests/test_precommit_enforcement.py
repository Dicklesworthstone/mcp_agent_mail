"""P2 Regression Tests: Pre-commit Guard Enforcement.

Test Cases:
1. Pre-commit blocks commits with conflicting exclusive reservations
2. Pre-commit allows commits when no conflicts exist
3. Pre-commit allows own reservations (same agent)
4. Pre-commit ignores shared (non-exclusive) reservations
5. Pre-commit ignores expired reservations
6. AGENT_MAIL_BYPASS=1 allows commit despite conflicts
7. AGENT_MAIL_GUARD_MODE=warn warns but allows commit
8. Missing AGENT_NAME returns error (exit 1)
9. Gate disabled (WORKTREES_ENABLED=0) exits early (exit 0)
10. Pattern matching with globs works correctly

Reference: mcp_agent_mail-irp
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import render_precommit_script
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record

# ============================================================================
# Helper Functions
# ============================================================================


def init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository with dummy user config."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def stage_file(repo_path: Path, rel_path: str, content: str = "test content") -> None:
    """Create and stage a file in the repo."""
    target = repo_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=str(repo_path), check=True)


def run_precommit_script(
    script_path: Path,
    repo_path: Path,
    agent_name: str | None = None,
    worktrees_enabled: str = "1",
    bypass: str = "0",
    guard_mode: str = "block",
) -> subprocess.CompletedProcess:
    """Run the pre-commit script with specified environment."""
    env = os.environ.copy()
    if agent_name:
        env["AGENT_NAME"] = agent_name
    elif "AGENT_NAME" in env:
        del env["AGENT_NAME"]
    env["WORKTREES_ENABLED"] = worktrees_enabled
    env["AGENT_MAIL_BYPASS"] = bypass
    env["AGENT_MAIL_GUARD_MODE"] = guard_mode
    return subprocess.run(
        ["python", str(script_path)],
        cwd=str(repo_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ============================================================================
# Test: Basic Conflict Detection
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_blocks_conflicting_exclusive_reservation(isolated_env, tmp_path: Path):
    """Pre-commit should block when staged files conflict with another agent's exclusive reservation."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-1")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write reservation held by another agent
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    # Setup code repo and stage conflicting file
    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # Should block (exit 1)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 1, f"Should block commit, stderr: {proc.stderr}"
    assert "conflict" in proc.stderr.lower(), "Should mention conflict in error"


@pytest.mark.asyncio
async def test_precommit_allows_when_no_conflicts(isolated_env, tmp_path: Path):
    """Pre-commit should allow commit when no conflicting reservations exist."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-2")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # No reservations created

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # Should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 0, f"Should allow commit, stderr: {proc.stderr}"


# ============================================================================
# Test: Own Reservations Don't Conflict
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_allows_own_reservation(isolated_env, tmp_path: Path):
    """Pre-commit should allow when the reservation is held by the same agent."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-3")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write reservation held by same agent
    await write_file_reservation_record(
        archive,
        {
            "agent": "SameAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # Same agent - should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="SameAgent")
    assert proc.returncode == 0, f"Should allow own reservation, stderr: {proc.stderr}"


# ============================================================================
# Test: Shared Reservations Don't Conflict
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_ignores_shared_reservation(isolated_env, tmp_path: Path):
    """Pre-commit should ignore non-exclusive (shared) reservations."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-4")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write shared (non-exclusive) reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": False,  # Shared reservation
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # Shared reservations shouldn't conflict - should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 0, f"Should ignore shared reservation, stderr: {proc.stderr}"


# ============================================================================
# Test: Expired Reservations Don't Conflict
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_ignores_expired_reservation(isolated_env, tmp_path: Path):
    """Pre-commit should ignore expired reservations."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-5")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write expired reservation
    expired_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
            "expires_ts": expired_ts,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # Expired reservation shouldn't conflict - should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 0, f"Should ignore expired reservation, stderr: {proc.stderr}"


# ============================================================================
# Test: Bypass Mode
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_bypass_allows_despite_conflict(isolated_env, tmp_path: Path):
    """AGENT_MAIL_BYPASS=1 should allow commit despite conflicts."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-6")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # With bypass=1, should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent", bypass="1")
    assert proc.returncode == 0, f"Bypass should allow commit, stderr: {proc.stderr}"
    assert "bypass" in proc.stderr.lower(), "Should mention bypass in output"


# ============================================================================
# Test: Warn Mode
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_warn_mode_allows_with_warning(isolated_env, tmp_path: Path):
    """AGENT_MAIL_GUARD_MODE=warn should warn but allow commit."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-7")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # With guard_mode=warn, should allow (exit 0) but print warning
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent", guard_mode="warn")
    assert proc.returncode == 0, f"Warn mode should allow commit, stderr: {proc.stderr}"
    # Warning should still be printed
    assert "conflict" in proc.stderr.lower(), "Should still warn about conflict"


# ============================================================================
# Test: Missing AGENT_NAME
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_missing_agent_name_fails(isolated_env, tmp_path: Path):
    """Pre-commit should fail if AGENT_NAME is not set."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-8")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # No AGENT_NAME - should fail (exit 1)
    proc = run_precommit_script(script_path, code_repo, agent_name=None)
    assert proc.returncode == 1, f"Missing AGENT_NAME should fail, stderr: {proc.stderr}"
    assert "agent_name" in proc.stderr.lower(), "Should mention AGENT_NAME requirement"


# ============================================================================
# Test: Gate Disabled
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_gate_disabled_exits_early(isolated_env, tmp_path: Path):
    """With WORKTREES_ENABLED=0, pre-commit should exit 0 without checking."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-9")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # With WORKTREES_ENABLED=0, should exit early without checking conflicts
    proc = run_precommit_script(
        script_path, code_repo, agent_name="TestAgent", worktrees_enabled="0"
    )
    assert proc.returncode == 0, f"Gate disabled should allow, stderr: {proc.stderr}"


# ============================================================================
# Test: Pattern Matching with Globs
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_glob_pattern_matches(isolated_env, tmp_path: Path):
    """Pre-commit should match glob patterns like src/** against src/app.py."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-10")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write reservation with glob pattern
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/**",  # Glob pattern
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/nested/deep/file.py")

    # Glob src/** should match src/nested/deep/file.py
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 1, f"Glob should match nested file, stderr: {proc.stderr}"


@pytest.mark.asyncio
async def test_precommit_glob_pattern_no_match(isolated_env, tmp_path: Path):
    """Pre-commit should not match unrelated paths."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-11")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write reservation with specific pattern
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/**",  # Only src/
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "tests/test_app.py")  # Not under src/

    # tests/test_app.py should not match src/**
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 0, f"Should not conflict with unrelated path, stderr: {proc.stderr}"


# ============================================================================
# Test: Multiple Conflicts
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_multiple_conflicts_reported(isolated_env, tmp_path: Path):
    """Pre-commit should report multiple conflicts when present."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-12")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write multiple reservations
    await write_file_reservation_record(
        archive,
        {
            "agent": "Agent1",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )
    await write_file_reservation_record(
        archive,
        {
            "agent": "Agent2",
            "path_pattern": "src/utils.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")
    stage_file(code_repo, "src/utils.py")

    # Should block and report both conflicts
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 1
    assert "app.py" in proc.stderr or "utils.py" in proc.stderr


# ============================================================================
# Test: No Staged Files
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_no_staged_files_allows(isolated_env, tmp_path: Path):
    """Pre-commit should allow when no files are staged."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-13")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write reservation (should not matter)
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/**",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    # Create file but don't stage it
    (code_repo / "src").mkdir(parents=True, exist_ok=True)
    (code_repo / "src" / "app.py").write_text("content", encoding="utf-8")
    # No git add

    # No staged files - should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent")
    assert proc.returncode == 0, f"No staged files should allow, stderr: {proc.stderr}"


# ============================================================================
# Test: Advisory Mode Synonyms
# ============================================================================


@pytest.mark.asyncio
async def test_precommit_advisory_mode_synonym(isolated_env, tmp_path: Path):
    """AGENT_MAIL_GUARD_MODE=advisory should work same as warn."""
    settings = get_settings()
    archive = await ensure_archive(settings, "enforcement-test-14")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    init_git_repo(code_repo)
    stage_file(code_repo, "src/app.py")

    # With guard_mode=advisory, should allow (exit 0)
    proc = run_precommit_script(script_path, code_repo, agent_name="TestAgent", guard_mode="advisory")
    assert proc.returncode == 0, f"Advisory mode should allow commit, stderr: {proc.stderr}"
