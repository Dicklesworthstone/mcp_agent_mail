import subprocess
from pathlib import Path

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def test_identity_reports_core_ignorecase(tmp_path: Path, monkeypatch) -> None:
    # Enable worktrees to exercise identity logic branches
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    # Set core.ignorecase true and verify
    _git(repo, "config", "core.ignorecase", "true")
    ident_true = _resolve_project_identity(str(repo))
    assert ident_true["core_ignorecase"] is True
    # Set core.ignorecase false and verify
    _git(repo, "config", "core.ignorecase", "false")
    ident_false = _resolve_project_identity(str(repo))
    assert ident_false["core_ignorecase"] is False

