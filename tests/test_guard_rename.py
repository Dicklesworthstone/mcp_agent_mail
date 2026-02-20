import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from mcp_agent_mail.guard import render_precommit_script
from mcp_agent_mail.storage import ProjectArchive


class _DummyArchive:
    def __init__(self, root: Path) -> None:
        self.root = root


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_precommit_blocks_on_rename_conflict(tmp_path: Path, monkeypatch) -> None:
    # Set up a tiny git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "foo.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "src/foo.txt")
    _git(repo, "commit", "-m", "init")
    # Rename staged
    _git(repo, "mv", "src/foo.txt", "src/bar.txt")
    # Archive with reservation
    archive_root = tmp_path / "archive" / "projects" / "projslug"
    reservations = archive_root / "file_reservations"
    reservations.mkdir(parents=True, exist_ok=True)
    res = {
        "agent": "Other",
        "exclusive": True,
        "path_pattern": "src/bar.txt",
        "expires_ts": _future_iso(),
    }
    (reservations / "r.json").write_text(json.dumps(res), encoding="utf-8")
    # Render and run hook in block mode
    hook = repo / "pre-commit-test.py"
    script = render_precommit_script(cast(ProjectArchive, _DummyArchive(archive_root)))
    hook.write_text(script, encoding="utf-8")
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "block"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run([sys.executable, str(hook)], cwd=str(repo), env=env).returncode
    assert rc == 1


def test_precommit_warns_on_rename_conflict_in_warn_mode(tmp_path: Path, monkeypatch) -> None:
    # Set up a tiny git repo
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "src/a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "mv", "src/a.txt", "src/b.txt")
    # Archive with reservation matching new name
    archive_root = tmp_path / "archive2" / "projects" / "projslug"
    reservations = archive_root / "file_reservations"
    reservations.mkdir(parents=True, exist_ok=True)
    res = {
        "agent": "Other",
        "exclusive": True,
        "path_pattern": "src/b.txt",
        "expires_ts": _future_iso(),
    }
    (reservations / "r.json").write_text(json.dumps(res), encoding="utf-8")
    hook = repo / "pre-commit-test.py"
    hook.write_text(render_precommit_script(cast(ProjectArchive, _DummyArchive(archive_root))), encoding="utf-8")
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "warn"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run([sys.executable, str(hook)], cwd=str(repo), env=env).returncode
    assert rc == 0
