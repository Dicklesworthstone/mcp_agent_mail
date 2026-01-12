import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

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


@pytest.mark.skipif(subprocess.call(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0, reason="git not available")
def test_cross_worktree_conflict_blocks_commit(tmp_path: Path) -> None:
    # Main repo
    main = tmp_path / "repo"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.name", "Unit Test")
    _git(main, "config", "user.email", "test@example.com")
    (main / "src").mkdir()
    (main / "src" / "shared.txt").write_text("v1\n", encoding="utf-8")
    _git(main, "add", "src/shared.txt")
    _git(main, "commit", "-m", "init")
    # Create a second worktree
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "feature/wt")
    # Create a reservation that should conflict in archive shared across worktrees
    archive_root = tmp_path / "archive" / "projects" / "slug"
    fr_dir = archive_root / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    (fr_dir / "lock.json").write_text(
        json.dumps({"agent": "Other", "exclusive": True, "path_pattern": "src/shared.txt", "expires_ts": _future_iso()}),
        encoding="utf-8",
    )
    # Stage a change in worktree and run pre-commit
    (wt / "src" / "shared.txt").write_text("v2\n", encoding="utf-8")
    _git(wt, "add", "src/shared.txt")
    hook = wt / "pre-commit-test.py"
    hook.write_text(render_precommit_script(cast(ProjectArchive, _DummyArchive(archive_root))), encoding="utf-8")
    env = os.environ.copy()
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_MAIL_GUARD_MODE"] = "block"
    env["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run([sys.executable, str(hook)], cwd=str(wt), env=env).returncode
    assert rc == 1
