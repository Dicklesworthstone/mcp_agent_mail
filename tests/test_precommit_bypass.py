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


def test_precommit_bypass_allows_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "c.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "src/c.txt")
    _git(repo, "commit", "-m", "init")
    # Stage a conflicting path
    (repo / "src" / "c.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "src/c.txt")
    # Create reservation
    archive_root = tmp_path / "archive" / "projects" / "slug"
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    (fr_dir / "r.json").write_text(
        json.dumps({"agent": "Other", "exclusive": True, "path_pattern": "src/c.txt", "expires_ts": _future_iso()}),
        encoding="utf-8",
    )
    # Run hook with bypass
    hook = repo / "pre-commit-test.py"
    hook.write_text(render_precommit_script(cast(ProjectArchive, _DummyArchive(archive_root))), encoding="utf-8")
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "block"
    env["AGENT_MAIL_BYPASS"] = "1"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run([sys.executable, str(hook)], cwd=str(repo), env=env).returncode
    assert rc == 0
