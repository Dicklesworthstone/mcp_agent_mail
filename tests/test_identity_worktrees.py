import subprocess
from pathlib import Path

import pytest

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


@pytest.mark.skipif(subprocess.call(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0, reason="git not available")
def test_identity_same_across_worktrees(tmp_path: Path, monkeypatch) -> None:
    # Enable worktrees and choose git-common-dir mode for stable identity across worktrees
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "git-common-dir")
    get_settings.cache_clear()

    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")

    wt = tmp_path / "wt1"
    _git(repo, "worktree", "add", str(wt), "-b", "feature/wt1")

    ident_main = _resolve_project_identity(str(repo))
    ident_wt = _resolve_project_identity(str(wt))
    assert ident_main["project_uid"] == ident_wt["project_uid"]
    assert ident_main["slug"] == ident_wt["slug"]

