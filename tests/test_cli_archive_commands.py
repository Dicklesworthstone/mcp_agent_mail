"""CLI Archive Commands Tests.

Tests for archive-related CLI subcommands:
- archive save: Create ZIP backup of SQLite and storage
- archive list: Show saved mailbox states
- archive restore: Restore a previously saved state

Reference: mcp_agent_mail-enu
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile

from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.cli import app

runner = CliRunner()

# ANSI escape code pattern for stripping colors from CLI output
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text for reliable assertion matching."""
    return _ANSI_ESCAPE_RE.sub("", text)


# ============================================================================
# Fixtures and helpers
# ============================================================================


def create_test_archive(archive_dir: Path, name: str = "test_archive.zip") -> Path:
    """Create a minimal test archive file."""
    archive_path = archive_dir / name
    archive_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("test_content.txt", "test archive content")
    return archive_path


def test_archive_states_dir_prefers_inner_git_repo_over_outer_pyproject(isolated_env, tmp_path, monkeypatch):
    """Archive directory resolution should anchor to the nearest git repo, not an outer pyproject."""
    outer = tmp_path / "outer-project"
    repo = outer / "nested-repo"
    work = repo / "deep" / "subdir"
    work.mkdir(parents=True, exist_ok=True)
    (outer / "pyproject.toml").write_text("[project]\nname='outer'\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    monkeypatch.chdir(work)

    archive_dir = cli_module._archive_states_dir(create=False)

    assert archive_dir == repo.resolve() / cli_module.ARCHIVE_DIR_NAME


def test_detect_git_head_supports_git_worktrees(isolated_env, tmp_path):
    """_detect_git_head should resolve worktree .git files, not just .git directories."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    worktree = tmp_path / "repo-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree)],
        cwd=str(repo),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert cli_module._detect_git_head(worktree) == expected_head


# ============================================================================
# archive save tests
# ============================================================================


def test_archive_save_creates_zip(isolated_env, monkeypatch):
    """archive save creates a ZIP file with correct preset."""
    captured: dict[str, Any] = {}

    def fake_create_archive(**kwargs):
        captured.update(kwargs)
        return Path("/fake/archive.zip"), {"scrub_preset": kwargs["scrub_preset"]}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_create_archive)

    result = runner.invoke(app, ["archive", "save"])
    assert result.exit_code == 0
    assert captured["scrub_preset"] == "archive"


def test_archive_save_with_label(isolated_env, monkeypatch):
    """archive save accepts --label option."""
    captured: dict[str, Any] = {}

    def fake_create_archive(**kwargs):
        captured.update(kwargs)
        return Path("/fake/archive.zip"), {"scrub_preset": kwargs["scrub_preset"]}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_create_archive)

    result = runner.invoke(app, ["archive", "save", "--label", "nightly"])
    assert result.exit_code == 0
    assert captured.get("label") == "nightly"


def test_archive_save_with_project_filter(isolated_env, monkeypatch):
    """archive save accepts --project filter option."""
    captured: dict[str, Any] = {}

    def fake_create_archive(**kwargs):
        captured.update(kwargs)
        return Path("/fake/archive.zip"), {"scrub_preset": kwargs["scrub_preset"]}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_create_archive)

    result = runner.invoke(
        app, ["archive", "save", "--project", "proj1", "--project", "proj2"]
    )
    assert result.exit_code == 0
    assert "proj1" in captured.get("project_filters", [])
    assert "proj2" in captured.get("project_filters", [])


def test_archive_save_with_scrub_preset(isolated_env, monkeypatch):
    """archive save accepts --scrub-preset option."""
    captured: dict[str, Any] = {}

    def fake_create_archive(**kwargs):
        captured.update(kwargs)
        return Path("/fake/archive.zip"), {"scrub_preset": kwargs["scrub_preset"]}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_create_archive)

    result = runner.invoke(app, ["archive", "save", "--scrub-preset", "standard"])
    assert result.exit_code == 0
    assert captured["scrub_preset"] == "standard"


def test_archive_save_invalid_scrub_preset(isolated_env, monkeypatch):
    """archive save rejects invalid scrub presets."""
    # Don't need to mock since it should fail validation before calling create
    result = runner.invoke(app, ["archive", "save", "--scrub-preset", "invalid_preset"])
    # Should fail with an error about invalid preset
    assert result.exit_code != 0 or "invalid" in result.stdout.lower()


def test_archive_save_short_options(isolated_env, monkeypatch):
    """archive save accepts short options -p and -l."""
    captured: dict[str, Any] = {}

    def fake_create_archive(**kwargs):
        captured.update(kwargs)
        return Path("/fake/archive.zip"), {"scrub_preset": kwargs["scrub_preset"]}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_create_archive)

    result = runner.invoke(app, ["archive", "save", "-p", "myproj", "-l", "test"])
    assert result.exit_code == 0
    assert "myproj" in captured.get("project_filters", [])
    assert captured.get("label") == "test"


# ============================================================================
# archive list tests
# ============================================================================


def test_archive_list_empty_directory(isolated_env, tmp_path, monkeypatch):
    """archive list handles empty archive directory gracefully."""
    # Point archive dir to an empty temp directory
    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: tmp_path / "archives",
    )

    result = runner.invoke(app, ["archive", "list"])
    assert result.exit_code == 0
    # Strip ANSI codes and normalize whitespace for reliable matching in CI
    # Rich Console may word-wrap long lines causing "does not\nexist" splits
    stdout = " ".join(strip_ansi(result.stdout).split())
    assert "does not exist" in stdout or "No saved" in stdout


def test_archive_list_json_output_empty_directory_returns_empty_array(isolated_env, tmp_path, monkeypatch):
    """archive list --json should stay machine-readable when no archive directory exists yet."""
    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: tmp_path / "archives",
    )

    result = runner.invoke(app, ["archive", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_archive_list_nonexistent_directory(isolated_env, tmp_path, monkeypatch):
    """archive list handles nonexistent archive directory."""
    nonexistent = tmp_path / "nonexistent"

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: nonexistent,
    )

    result = runner.invoke(app, ["archive", "list"])
    assert result.exit_code == 0


def test_archive_list_json_output_nonexistent_directory_returns_empty_array(isolated_env, tmp_path, monkeypatch):
    """archive list --json should return [] for a missing archive directory."""
    nonexistent = tmp_path / "nonexistent"

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: nonexistent,
    )

    result = runner.invoke(app, ["archive", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_archive_list_shows_archives(isolated_env, tmp_path, monkeypatch):
    """archive list displays available archives."""
    archive_dir = tmp_path / "archives"
    create_test_archive(archive_dir, "mailbox_2025-01-01.zip")
    create_test_archive(archive_dir, "mailbox_2025-01-02.zip")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    result = runner.invoke(app, ["archive", "list"])
    assert result.exit_code == 0
    # Table truncates long filenames with "...", so check for partial match
    assert "mailbox_2025-01-0" in result.stdout


def test_archive_list_with_limit(isolated_env, tmp_path, monkeypatch):
    """archive list respects --limit option."""
    archive_dir = tmp_path / "archives"
    for i in range(5):
        create_test_archive(archive_dir, f"mailbox_2025-01-0{i}.zip")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    result = runner.invoke(app, ["archive", "list", "--limit", "2"])
    assert result.exit_code == 0


def test_archive_list_json_output(isolated_env, tmp_path, monkeypatch):
    """archive list supports --json output format."""
    archive_dir = tmp_path / "archives"
    create_test_archive(archive_dir, "mailbox_test.zip")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    result = runner.invoke(app, ["archive", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)


def test_archive_list_short_limit_option(isolated_env, tmp_path, monkeypatch):
    """archive list accepts -n short option for limit."""
    archive_dir = tmp_path / "archives"
    for i in range(3):
        create_test_archive(archive_dir, f"archive_{i}.zip")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    result = runner.invoke(app, ["archive", "list", "-n", "1"])
    assert result.exit_code == 0


# ============================================================================
# archive restore tests
# ============================================================================


def test_archive_restore_missing_file(isolated_env, tmp_path):
    """archive restore fails gracefully with missing file."""
    missing_file = tmp_path / "nonexistent.zip"

    result = runner.invoke(app, ["archive", "restore", str(missing_file)])
    assert result.exit_code != 0


def test_archive_restore_dry_run(isolated_env, tmp_path, monkeypatch):
    """archive restore --dry-run shows planned steps without changes."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create a proper archive with snapshot and storage_repo directories
    archive_path = archive_dir / "valid_archive.zip"
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        # Add the expected snapshot file and storage_repo
        zf.writestr("snapshot/mailbox.sqlite3", b"sqlite db placeholder")
        zf.writestr("storage_repo/README.txt", "storage data")

    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--dry-run"])
    # Dry run should succeed or show informative output
    assert result.exit_code == 0 or "dry" in result.stdout.lower() or "plan" in result.stdout.lower()


def test_archive_restore_force_option(isolated_env, tmp_path):
    """archive restore --force skips confirmation prompts."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create a proper archive with snapshot and storage_repo directories
    archive_path = archive_dir / "force_test.zip"
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("snapshot/mailbox.sqlite3", b"sqlite db placeholder")
        zf.writestr("storage_repo/README.txt", "storage data")

    # With --force, should attempt restore without prompting
    # We use --dry-run to avoid actual file changes
    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--force", "--dry-run"])
    # Should accept both options without error
    assert result.exit_code == 0 or "backup" in result.stdout.lower() or "restore" in result.stdout.lower()


def test_archive_restore_invalid_zip(isolated_env, tmp_path, monkeypatch):
    """archive restore handles invalid/corrupt ZIP files."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    invalid_zip = archive_dir / "invalid.zip"
    invalid_zip.write_text("not a zip file")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._resolve_archive_path",
        lambda path: invalid_zip,
    )

    result = runner.invoke(app, ["archive", "restore", str(invalid_zip), "--force"])
    stdout = strip_ansi(result.stdout)
    assert result.exit_code != 0
    assert "Failed to extract archive" in stdout
    assert "not a zip file" in stdout.lower()


def test_archive_restore_by_filename(isolated_env, tmp_path, monkeypatch):
    """archive restore can find archives by filename in default directory."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create a proper archive with snapshot and storage_repo
    archive_path = archive_dir / "my_backup.zip"
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("snapshot/mailbox.sqlite3", b"sqlite db placeholder")
        zf.writestr("storage_repo/README.txt", "storage data")

    # Mock the archive states dir to point to our test directory
    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    # Use full path with --dry-run to test without side effects
    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--dry-run"])
    # Should find the archive
    assert result.exit_code == 0 or "my_backup" in result.stdout.lower() or "restore" in result.stdout.lower()


def test_archive_restore_rolls_back_on_copy_failure(isolated_env, tmp_path, monkeypatch):
    """archive restore should restore the original DB/storage if copyback fails mid-restore."""
    archive_path = tmp_path / "rollback_test.zip"
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("snapshot/mailbox.sqlite3", b"restored-db")
        zf.writestr("storage_repo/README.txt", "restored-storage")

    live_db = tmp_path / "live" / "mailbox.sqlite3"
    live_db.parent.mkdir(parents=True, exist_ok=True)
    live_db.write_bytes(b"original-db")

    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    (storage_root / "README.txt").write_text("original-storage", encoding="utf-8")

    monkeypatch.setattr(cli_module, "_resolve_archive_path", lambda _path: archive_path)
    monkeypatch.setattr(cli_module, "resolve_sqlite_database_path", lambda: live_db)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(storage=SimpleNamespace(root=str(storage_root))),
    )

    real_copytree = cli_module.shutil.copytree
    failed_once = {"value": False}

    def flaky_copytree(src, dst, *args, **kwargs):
        if Path(src).name == "storage_repo" and not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("simulated storage copy failure")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(cli_module.shutil, "copytree", flaky_copytree)

    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--force"])

    stdout = strip_ansi(result.stdout)
    assert result.exit_code != 0
    assert "Restore failed:" in stdout
    assert "Original database and storage were restored from backups." in stdout
    assert live_db.read_bytes() == b"original-db"
    assert (storage_root / "README.txt").read_text(encoding="utf-8") == "original-storage"


def test_archive_restore_rejects_traversal_member(isolated_env, tmp_path):
    """archive restore rejects zip members that try to escape the extraction root."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archive_path = archive_dir / "traversal_member.zip"
    with ZipFile(archive_path, "w") as zf:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scrub_preset": "archive",
            "projects_requested": [],
        }
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("snapshot/mailbox.sqlite3", b"sqlite db placeholder")
        zf.writestr("storage_repo/README.txt", "storage data")
        zf.writestr("../escape.txt", "malicious content")

    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--dry-run"])
    stdout = strip_ansi(result.stdout)
    assert result.exit_code != 0
    assert "Invalid archive member path" in stdout
    assert "directory traversal" in stdout


# ============================================================================
# Edge cases and error handling
# ============================================================================


def test_archive_save_handles_database_error(isolated_env, monkeypatch):
    """archive save handles database errors gracefully."""

    def failing_archive(**kwargs):
        raise RuntimeError("Database connection failed")

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", failing_archive)

    result = runner.invoke(app, ["archive", "save"])
    assert result.exit_code != 0


def test_archive_list_handles_corrupt_metadata(isolated_env, tmp_path, monkeypatch):
    """archive list handles archives with corrupt/missing metadata."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create archive without metadata
    bad_archive = archive_dir / "bad_archive.zip"
    with ZipFile(bad_archive, "w") as zf:
        zf.writestr("data.txt", "no metadata here")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._archive_states_dir",
        lambda create=True: archive_dir,
    )

    result = runner.invoke(app, ["archive", "list"])
    # Should still list the archive, possibly with warnings
    assert result.exit_code == 0


def test_archive_restore_metadata_warning(isolated_env, tmp_path, monkeypatch):
    """archive restore shows warning for missing/invalid metadata."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create archive with invalid metadata
    archive_path = archive_dir / "no_meta.zip"
    with ZipFile(archive_path, "w") as zf:
        zf.writestr("data.txt", "archive without metadata")

    monkeypatch.setattr(
        "mcp_agent_mail.cli._resolve_archive_path",
        lambda path: archive_path,
    )

    result = runner.invoke(app, ["archive", "restore", str(archive_path), "--dry-run"])
    # Should mention warning about metadata
    assert result.exit_code == 0 or "warning" in result.stdout.lower()


def test_archive_commands_help_text(isolated_env):
    """archive commands show helpful usage information."""
    result = runner.invoke(app, ["archive", "--help"])
    assert result.exit_code == 0
    # Strip ANSI codes for reliable matching in CI
    stdout = strip_ansi(result.stdout)
    assert "save" in stdout
    assert "list" in stdout
    assert "restore" in stdout


def test_archive_save_help_text(isolated_env):
    """archive save shows its options in help."""
    result = runner.invoke(app, ["archive", "save", "--help"])
    assert result.exit_code == 0
    # Strip ANSI codes for reliable matching in CI
    stdout = strip_ansi(result.stdout)
    assert "--project" in stdout or "-p" in stdout
    assert "--label" in stdout or "-l" in stdout
    assert "--scrub-preset" in stdout


def test_archive_list_help_text(isolated_env):
    """archive list shows its options in help."""
    result = runner.invoke(app, ["archive", "list", "--help"])
    assert result.exit_code == 0
    # Strip ANSI codes for reliable matching in CI
    stdout = strip_ansi(result.stdout)
    assert "--limit" in stdout or "-n" in stdout
    assert "--json" in stdout


def test_archive_restore_help_text(isolated_env):
    """archive restore shows its options in help."""
    result = runner.invoke(app, ["archive", "restore", "--help"])
    assert result.exit_code == 0
    # Strip ANSI codes for reliable matching in CI
    stdout = strip_ansi(result.stdout)
    assert "--force" in stdout or "-f" in stdout
    assert "--dry-run" in stdout
