"""Tests for guard hook functionality in git worktree scenarios.

Tests guard installation, hook generation, and conflict detection in various
git configurations including worktrees, custom hooksPath, and hook preservation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import (
    install_guard,
    install_prepush_guard,
    render_precommit_script,
    render_prepush_script,
    uninstall_guard,
)
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def _create_initial_commit(repo_path: Path) -> None:
    """Create an initial commit in the repo."""
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_path), check=True, capture_output=True)


def _create_worktree(main_repo: Path, worktree_path: Path, branch_name: str) -> None:
    """Create a git worktree."""
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
    )


def _run_hook(hook_path: Path, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run a hook script."""
    full_env = os.environ.copy()
    full_env.update(env)
    return subprocess.run(
        ["python", str(hook_path)],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )


def _git_add(repo_path: Path, file_path: str) -> None:
    """Stage a file in a git repository."""
    subprocess.run(["git", "add", file_path], cwd=str(repo_path), check=True)


def _git_config(repo_path: Path, key: str, value: str) -> None:
    """Set a git config value."""
    subprocess.run(["git", "config", key, value], cwd=str(repo_path), check=True)


# =============================================================================
# Basic Worktree Installation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_in_worktree(isolated_env, tmp_path: Path):
    """Test guard installation in a git worktree."""
    settings = get_settings()

    # Create main repo with initial commit
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Install guard in worktree
    await ensure_archive(settings, "worktree-test")
    hook_path = await install_guard(settings, "worktree-test", worktree)

    # Hook should be installed in the worktree's git dir
    assert hook_path.exists()
    assert "pre-commit" in hook_path.name


@pytest.mark.asyncio
async def test_guard_conflict_detection_in_worktree(isolated_env, tmp_path: Path):
    """Test that guard detects conflicts in worktree context."""
    settings = get_settings()

    # Create main repo
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Create archive with file reservation
    archive = await ensure_archive(settings, "worktree-test")
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/*.py",
            "exclusive": True,
        },
    )

    # Render and write the guard script
    script = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script, encoding="utf-8")

    # Stage a conflicting file
    src_dir = worktree / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(worktree, "src/app.py")

    # Run the guard script with WORKTREES_ENABLED
    result = _run_hook(
        script_path,
        worktree,
        {"AGENT_NAME": "MyAgent", "WORKTREES_ENABLED": "1"},
    )

    # Should detect conflict
    assert result.returncode == 1
    assert "conflict" in result.stderr.lower() or "file_reservation" in result.stderr.lower()


# =============================================================================
# Custom core.hooksPath Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_custom_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with custom core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set custom hooksPath
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir(parents=True)
    _git_config(repo, "core.hooksPath", str(custom_hooks))

    # Install guard
    hook_path = await install_guard(settings, "hookspath-test", repo)

    # Hook should be in custom hooks directory
    assert hook_path.parent == custom_hooks or str(custom_hooks) in str(hook_path)


@pytest.mark.asyncio
async def test_guard_install_relative_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with relative core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set relative hooksPath
    (repo / "my-hooks").mkdir(parents=True)
    _git_config(repo, "core.hooksPath", "my-hooks")

    # Install guard
    hook_path = await install_guard(settings, "rel-hookspath-test", repo)

    # Hook should be resolved relative to repo root
    assert hook_path.exists()


# =============================================================================
# Hook Preservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_preserves_existing_hook(isolated_env, tmp_path: Path):
    """Test that guard preserves existing pre-commit hook as .orig."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'existing hook'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard
    await install_guard(settings, "preserve-test", repo)

    # Original hook should be preserved as .orig
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    assert "existing hook" in orig_hook.read_text()


@pytest.mark.asyncio
async def test_guard_doesnt_overwrite_own_orig(isolated_env, tmp_path: Path):
    """Test that reinstalling guard doesn't overwrite .orig file."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'original'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard first time
    await install_guard(settings, "preserve-test", repo)

    # Verify .orig was created
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    original_content = orig_hook.read_text()

    # Install guard second time
    await install_guard(settings, "preserve-test", repo)

    # .orig should still have original content
    assert orig_hook.read_text() == original_content


# =============================================================================
# Gate Variations Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_true(isolated_env, tmp_path: Path):
    """Test guard runs when WORKTREES_ENABLED=1."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=1
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Should run (no conflicts, so exit 0)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_false(isolated_env, tmp_path: Path):
    """Test guard exits early when WORKTREES_ENABLED=0."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")

    # Add a conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.txt",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file that would conflict
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=0 (disabled)
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "0"})

    # Should exit early with 0 (no conflict check)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_git_identity_enabled(isolated_env, tmp_path: Path):
    """Test guard runs when GIT_IDENTITY_ENABLED=1 (alternative gate)."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with GIT_IDENTITY_ENABLED=1 (alternative gate)
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "GIT_IDENTITY_ENABLED": "1"})

    # Should run
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_various_true_values(isolated_env, tmp_path: Path):
    """Test guard recognizes various truthy values for gate."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Test various truthy values
    for value in ["1", "true", "True", "TRUE", "yes", "Yes", "t", "T", "y", "Y"]:
        result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": value})
        # All should run (return 0 for no conflicts)
        assert result.returncode == 0, f"Gate value '{value}' should be truthy"


# =============================================================================
# Advisory Mode Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_advisory_mode_warn(isolated_env, tmp_path: Path):
    """Test guard in advisory/warn mode doesn't block on conflicts."""
    settings = get_settings()
    archive = await ensure_archive(settings, "advisory-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run in advisory mode
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_GUARD_MODE": "warn",
        },
    )

    # Should exit 0 in advisory mode (warn but don't block)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_bypass_flag(isolated_env, tmp_path: Path):
    """Test AGENT_MAIL_BYPASS=1 bypasses all checks."""
    settings = get_settings()
    archive = await ensure_archive(settings, "bypass-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run with bypass enabled
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_BYPASS": "1",
        },
    )

    # Should bypass all checks
    assert result.returncode == 0
    assert "bypass" in result.stderr.lower()


# =============================================================================
# Pre-push Guard Tests
# =============================================================================


@pytest.mark.asyncio
async def test_prepush_guard_install(isolated_env, tmp_path: Path):
    """Test pre-push guard installation."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install pre-push guard
    hook_path = await install_prepush_guard(settings, "prepush-test", repo)

    assert hook_path.exists()
    assert "pre-push" in hook_path.name


@pytest.mark.asyncio
async def test_prepush_script_generation(isolated_env, tmp_path: Path):
    """Test pre-push script includes STDIN handling."""
    settings = get_settings()
    archive = await ensure_archive(settings, "prepush-test")

    script = render_prepush_script(archive)

    # Should have pre-push specific handling
    assert "pre-push" in script
    assert "stdin" in script.lower() or "STDIN" in script


# =============================================================================
# Uninstall Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_uninstall(isolated_env, tmp_path: Path):
    """Test guard uninstall removes hooks properly."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard
    await install_guard(settings, "uninstall-test", repo)

    # Uninstall
    removed = await uninstall_guard(repo)

    assert removed is True


@pytest.mark.asyncio
async def test_guard_uninstall_nonexistent(isolated_env, tmp_path: Path):
    """Test uninstall on repo without guard returns False."""
    # Create repo without guard
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Uninstall (nothing to remove)
    removed = await uninstall_guard(repo)

    assert removed is False


# =============================================================================
# Chain Runner Tests
# =============================================================================


@pytest.mark.asyncio
async def test_chain_runner_executes_plugins(isolated_env, tmp_path: Path):
    """Test chain runner executes plugins in hooks.d directory."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard (creates chain runner)
    hook_path = await install_guard(settings, "chain-test", repo)

    # Create additional plugin in hooks.d
    hooks_d = hook_path.parent / "hooks.d" / "pre-commit"
    hooks_d.mkdir(parents=True, exist_ok=True)

    # Plugin that creates a marker file
    plugin = hooks_d / "99-test-plugin.py"
    marker_file = tmp_path / "plugin_ran.txt"
    plugin.write_text(
        f"#!/usr/bin/env python3\n"
        f"from pathlib import Path\n"
        f"Path('{marker_file}').write_text('ran')\n",
        encoding="utf-8",
    )
    plugin.chmod(0o755)

    # Stage a file
    (repo / "test.txt").write_text("test", encoding="utf-8")
    _git_add(repo, "test.txt")

    # Run chain runner
    _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Plugin should have run
    assert marker_file.exists()
    assert marker_file.read_text() == "ran"
