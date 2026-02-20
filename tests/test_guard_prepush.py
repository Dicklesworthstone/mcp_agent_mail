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
    def __init__(self, root: Path) -> None:
        self.root = root


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_prepush_blocks_on_conflict_with_real_range(tmp_path: Path) -> None:
    # Create bare remote
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    # Create local repo and set origin
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "src").mkdir()
    (repo / "src" / "x.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "src/x.txt")
    _git(repo, "commit", "-m", "add x")
    # Create second commit that should conflict with reservation
    (repo / "src" / "x.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "src/x.txt")
    _git(repo, "commit", "-m", "update x")
    # Make reservation matching src/x.txt
    archive_root = tmp_path / "archive" / "projects" / "slug"
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    (fr_dir / "lock.json").write_text(
        json.dumps({"agent": "Other", "exclusive": True, "path_pattern": "src/x.txt", "expires_ts": _future_iso()}),
        encoding="utf-8",
    )
    # Render pre-push hook and run it providing pre-push stdin tuple
    hook = repo / "pre-push-test.py"
    hook.write_text(render_prepush_script(cast(ProjectArchive, _DummyArchive(archive_root))), encoding="utf-8")
    # Determine local ref and sha; remote has no refs yet
    local_ref = "refs/heads/main"
    from contextlib import suppress
    with suppress(Exception):
        # Ensure branch named main even if default differs
        _git(repo, "branch", "-M", "main")
    local_sha = _git(repo, "rev-parse", "HEAD")
    remote_ref = "refs/heads/main"
    remote_sha = "0" * 40
    stdin_payload = f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "block"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run(
        [sys.executable, str(hook), "origin"],
        cwd=str(repo),
        env=env,
        input=stdin_payload,
        text=True,
    ).returncode
    assert rc == 1


def test_prepush_warns_on_conflict_in_warn_mode(tmp_path: Path) -> None:
    remote = tmp_path / "remote2.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "src").mkdir()
    (repo / "src" / "y.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "src/y.txt")
    _git(repo, "commit", "-m", "add y")
    archive_root = tmp_path / "archive2" / "projects" / "slug"
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    (fr_dir / "lock.json").write_text(
        json.dumps({"agent": "Other", "exclusive": True, "path_pattern": "src/y.txt", "expires_ts": _future_iso()}),
        encoding="utf-8",
    )
    hook = repo / "pre-push-test.py"
    hook.write_text(render_prepush_script(cast(ProjectArchive, _DummyArchive(archive_root))), encoding="utf-8")
    _git(repo, "branch", "-M", "main")
    local_ref = "refs/heads/main"
    local_sha = _git(repo, "rev-parse", "HEAD")
    stdin_payload = f"{local_ref} {local_sha} refs/heads/main {'0'*40}\n"
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "warn"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run(
        [sys.executable, str(hook), "origin"],
        cwd=str(repo),
        env=env,
        input=stdin_payload,
        text=True,
    ).returncode
    assert rc == 0


def test_prepush_fallback_matches_backslash_pattern(tmp_path: Path) -> None:
    """Fallback fnmatch should normalize backslashes in reservation patterns."""
    remote = tmp_path / "remote3.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo3"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "src" / "nested").mkdir(parents=True)
    z_path = repo / "src" / "nested" / "z.txt"
    z_path.write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "src/nested/z.txt")
    _git(repo, "commit", "-m", "add z")
    # Second commit so diff-tree has a parent to compare against
    z_path.write_text("hi again\n", encoding="utf-8")
    _git(repo, "add", "src/nested/z.txt")
    _git(repo, "commit", "-m", "update z")

    archive_root = tmp_path / "archive3" / "projects" / "slug"
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    (fr_dir / "lock.json").write_text(
        json.dumps({"agent": "Other", "exclusive": True, "path_pattern": "src\\**\\z.txt", "expires_ts": _future_iso()}),
        encoding="utf-8",
    )

    hook = repo / "pre-push-test.py"
    script = render_prepush_script(cast(ProjectArchive, _DummyArchive(archive_root)))
    # Force fallback path (no pathspec) to exercise fnmatch normalization
    script = script.replace("if _PS and _GWM:", "if False and _GWM:")
    hook.write_text(script, encoding="utf-8")

    _git(repo, "branch", "-M", "main")
    local_sha = _git(repo, "rev-parse", "HEAD")
    stdin_payload = f"refs/heads/main {local_sha} refs/heads/main {'0'*40}\n"
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "block"
    env["AGENT_NAME"] = "BlueLake"
    rc = subprocess.run(
        [sys.executable, str(hook), "origin"],
        cwd=str(repo),
        env=env,
        input=stdin_payload,
        text=True,
    ).returncode
    assert rc == 1
