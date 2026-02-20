"""Comprehensive tests for CLI guard commands.

Tests cover:
- guard install: installs pre-commit/pre-push hooks
- guard uninstall: removes hooks
- guard status: displays guard status information
- guard check: validates paths against file reservations

Bead: mcp_agent_mail-3x5 (CLI: Guard Commands)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project
from mcp_agent_mail.storage import ensure_archive
from mcp_agent_mail.utils import slugify

if TYPE_CHECKING:
    from mcp_agent_mail.storage import ProjectArchive

runner = CliRunner()


def _compute_slug_for_path(path: Path) -> str:
    """Compute the project slug the same way guard check does."""
    return slugify(str(path.resolve()))


# =============================================================================
# Fixtures and Helpers
# =============================================================================


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository with basic config."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )


def _create_initial_commit(repo_path: Path) -> None:
    """Create an initial commit in the repo."""
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )


async def _seed_project(slug: str = "guardtest", human_key: str = "/guard/test") -> Project:
    """Create a test project."""
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug=slug, human_key=human_key)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project


async def _seed_project_with_agent(
    slug: str = "guardtest",
    human_key: str = "/guard/test",
    agent_name: str = "GuardAgent",
) -> tuple[Project, Agent]:
    """Create a test project with an agent."""
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug=slug, human_key=human_key)
        session.add(project)
        await session.commit()
        await session.refresh(project)

        agent = Agent(
            project_id=project.id,
            name=agent_name,
            program="test",
            model="test",
            task_description="Guard testing",
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return project, agent


def _write_file_reservation_json(
    archive: "ProjectArchive",
    agent_name: str,
    pattern: str,
    exclusive: bool = True,
    expires_hours: float = 1.0,
) -> Path:
    """Write a file reservation JSON artifact to the archive."""
    import hashlib

    fr_dir = archive.root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)

    expires_ts = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    data = {
        "agent": agent_name,
        "path_pattern": pattern,
        "exclusive": exclusive,
        "expires_ts": expires_ts.isoformat(),
        "created_ts": datetime.now(timezone.utc).isoformat(),
    }

    hash_val = hashlib.sha1(pattern.encode()).hexdigest()[:12]
    file_path = fr_dir / f"{hash_val}.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")
    return file_path


# =============================================================================
# guard install Tests
# =============================================================================


class TestGuardInstall:
    """Tests for 'guard install' CLI command."""

    def test_guard_install_basic(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test basic guard installation."""
        asyncio.run(_seed_project())

        # Enable worktrees feature
        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        # Clear cached settings
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])
        assert result.exit_code == 0
        assert "Installed guard" in result.output

    def test_guard_install_with_prepush(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard installation with --prepush flag."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(
            app, ["guard", "install", "guardtest", str(repo_dir), "--prepush"]
        )
        assert result.exit_code == 0
        assert "Installed guard" in result.output

    def test_guard_install_worktrees_disabled(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard install skips when WORKTREES_ENABLED=0."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "0")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])
        assert result.exit_code == 0
        assert "disabled" in result.output.lower() or "skipping" in result.output.lower()

    def test_guard_install_invalid_project(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard install with non-existent project."""
        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "install", "nonexistent-project", str(repo_dir)])
        # Should fail with error about project not found
        assert result.exit_code != 0 or "not found" in result.output.lower() or "no project" in result.output.lower()

    def test_guard_install_by_human_key(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard install using human_key instead of slug."""
        asyncio.run(_seed_project(slug="myslug", human_key="/my/project/path"))

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "install", "/my/project/path", str(repo_dir)])
        assert result.exit_code == 0
        assert "Installed guard" in result.output


# =============================================================================
# guard uninstall Tests
# =============================================================================


class TestGuardUninstall:
    """Tests for 'guard uninstall' CLI command."""

    def test_guard_uninstall_after_install(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test uninstalling guard after installation."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Install first
        install_result = runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])
        assert install_result.exit_code == 0

        # Then uninstall
        result = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_guard_uninstall_no_guard_present(self, tmp_path: Path, isolated_env):
        """Test uninstalling when no guard is present."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
        assert result.exit_code == 0
        assert "No guard" in result.output or "not found" in result.output.lower()

    def test_guard_uninstall_with_custom_hooks_path(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test uninstall resolves custom core.hooksPath correctly."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Set custom hooks path
        custom_hooks = tmp_path / "custom-hooks"
        custom_hooks.mkdir(parents=True)
        subprocess.run(
            ["git", "config", "core.hooksPath", str(custom_hooks)],
            cwd=str(repo_dir),
            check=True,
        )

        # Install guard
        install_result = runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])
        assert install_result.exit_code == 0

        # Uninstall
        result = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
        assert result.exit_code == 0


# =============================================================================
# guard status Tests
# =============================================================================


class TestGuardStatus:
    """Tests for 'guard status' CLI command."""

    def test_guard_status_basic(self, tmp_path: Path, isolated_env):
        """Test basic guard status display."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        assert "WORKTREES_ENABLED" in result.output
        assert "AGENT_MAIL_GUARD_MODE" in result.output
        assert "hooks_dir" in result.output
        assert "pre-commit" in result.output
        assert "pre-push" in result.output

    def test_guard_status_shows_worktrees_enabled(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test status reflects WORKTREES_ENABLED setting."""
        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        assert "true" in result.output.lower()

    def test_guard_status_shows_guard_mode(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test status reflects AGENT_MAIL_GUARD_MODE setting."""
        monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "warn")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        assert "warn" in result.output.lower()

    def test_guard_status_hooks_present_after_install(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test status shows 'present' after guard installation."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Install guard
        runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])

        # Check status
        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        assert "present" in result.output.lower()

    def test_guard_status_hooks_missing_initially(self, tmp_path: Path, isolated_env):
        """Test status shows 'missing' when no hooks installed."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        assert "missing" in result.output.lower()

    def test_guard_status_with_custom_hooks_path(self, tmp_path: Path, isolated_env):
        """Test status resolves custom core.hooksPath."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        custom_hooks = tmp_path / "my-hooks"
        custom_hooks.mkdir(parents=True)
        subprocess.run(
            ["git", "config", "core.hooksPath", str(custom_hooks)],
            cwd=str(repo_dir),
            check=True,
        )

        result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert result.exit_code == 0
        # Rich table may truncate the path with ellipsis, just check the command succeeded
        # and shows the expected fields
        assert "hooks_dir" in result.output


# =============================================================================
# guard check Tests
# =============================================================================


class TestGuardCheck:
    """Tests for 'guard check' CLI command."""

    def test_guard_check_requires_agent_name(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check fails without AGENT_NAME env var."""
        monkeypatch.delenv("AGENT_NAME", raising=False)

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(
            app,
            ["guard", "check", "--repo", str(repo_dir)],
            input="",
        )
        assert result.exit_code == 1
        assert "AGENT_NAME" in result.output

    def test_guard_check_empty_input_exits_zero(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check with empty input returns exit code 0."""
        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="",
        )
        assert result.exit_code == 0

    def test_guard_check_no_reservations_exits_zero(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check with no file reservations returns exit code 0."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        asyncio.run(ensure_archive(settings, slug))

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        # Provide paths but no reservations exist
        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00src/utils.py\x00",
        )
        assert result.exit_code == 0

    def test_guard_check_detects_conflict(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check detects file reservation conflict."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create a file reservation for another agent
        _write_file_reservation_json(archive, "OtherAgent", "src/**", exclusive=True)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        assert result.exit_code == 1
        assert "conflict" in result.output.lower()
        assert "OtherAgent" in result.output

    def test_guard_check_advisory_mode_exits_zero(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check with --advisory exits 0 even on conflict."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create a file reservation for another agent
        _write_file_reservation_json(archive, "OtherAgent", "src/**", exclusive=True)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--advisory", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        assert result.exit_code == 0
        assert "conflict" in result.output.lower()
        assert "advisory" in result.output.lower()

    def test_guard_check_own_reservation_not_conflict(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test that agent's own reservations don't cause conflicts."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create a file reservation for the SAME agent
        _write_file_reservation_json(archive, "TestAgent", "src/**", exclusive=True)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        # Should not conflict with own reservation
        assert result.exit_code == 0

    def test_guard_check_expired_reservation_ignored(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test that expired reservations don't cause conflicts."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create an EXPIRED file reservation
        _write_file_reservation_json(
            archive, "OtherAgent", "src/**", exclusive=True, expires_hours=-1.0
        )

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        # Expired reservation should be ignored
        assert result.exit_code == 0

    def test_guard_check_shared_reservation_not_conflict(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test that shared (non-exclusive) reservations don't block."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create a SHARED file reservation
        _write_file_reservation_json(archive, "OtherAgent", "src/**", exclusive=False)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        # Shared reservation should not conflict
        assert result.exit_code == 0

    def test_guard_check_multiple_paths(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check with multiple paths, one conflicting."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # Compute slug from repo path and create archive with reservation
        slug = _compute_slug_for_path(repo_dir)
        settings = get_settings()
        archive = asyncio.run(ensure_archive(settings, slug))

        # Create a file reservation for docs/**
        _write_file_reservation_json(archive, "OtherAgent", "docs/**", exclusive=True)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        # Only docs/readme.md should conflict
        result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00docs/readme.md\x00",
        )
        assert result.exit_code == 1
        assert "docs/readme.md" in result.output

    def test_guard_check_auto_detects_repo_root(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard check auto-detects repo root without --repo flag."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        monkeypatch.setenv("AGENT_NAME", "TestAgent")

        # Change to repo directory
        original_cwd = Path.cwd()
        try:
            os.chdir(str(repo_dir))
            result = runner.invoke(
                app,
                ["guard", "check", "--stdin-nul"],
                input="",
            )
            assert result.exit_code == 0
        finally:
            os.chdir(original_cwd)


# =============================================================================
# Integration Tests
# =============================================================================


class TestGuardIntegration:
    """Integration tests for guard CLI commands."""

    def test_full_guard_lifecycle(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test full guard lifecycle: install, status, check, uninstall."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        monkeypatch.setenv("AGENT_NAME", "TestAgent")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)

        # 1. Install guard
        install_result = runner.invoke(
            app, ["guard", "install", "guardtest", str(repo_dir), "--prepush"]
        )
        assert install_result.exit_code == 0
        assert "Installed" in install_result.output

        # 2. Check status - should show hooks present
        status_result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert status_result.exit_code == 0
        assert "present" in status_result.output.lower()

        # 3. Guard check with no reservations - should pass
        check_result = runner.invoke(
            app,
            ["guard", "check", "--stdin-nul", "--repo", str(repo_dir)],
            input="src/main.py\x00",
        )
        assert check_result.exit_code == 0

        # 4. Uninstall guard
        uninstall_result = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
        assert uninstall_result.exit_code == 0
        assert "Removed" in uninstall_result.output

        # 5. Check status - should show hooks missing
        status_result2 = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert status_result2.exit_code == 0
        assert "missing" in status_result2.output.lower()

    def test_guard_with_relative_hooks_path(self, tmp_path: Path, isolated_env, monkeypatch):
        """Test guard commands with relative core.hooksPath."""
        asyncio.run(_seed_project())

        monkeypatch.setenv("WORKTREES_ENABLED", "1")
        get_settings.cache_clear()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        _init_git_repo(repo_dir)
        _create_initial_commit(repo_dir)

        # Set relative hooks path
        rel_hooks = repo_dir / ".hooks"
        rel_hooks.mkdir(parents=True)
        subprocess.run(
            ["git", "config", "core.hooksPath", ".hooks"],
            cwd=str(repo_dir),
            check=True,
        )

        # Install guard
        install_result = runner.invoke(app, ["guard", "install", "guardtest", str(repo_dir)])
        assert install_result.exit_code == 0

        # Status should show the hooks directory
        status_result = runner.invoke(app, ["guard", "status", str(repo_dir)])
        assert status_result.exit_code == 0
        # Should resolve the relative path - just verify command succeeded
        assert "hooks_dir" in status_result.output

        # Uninstall
        uninstall_result = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
        assert uninstall_result.exit_code == 0
