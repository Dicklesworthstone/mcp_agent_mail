import hashlib
import subprocess
from pathlib import Path

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def _expect_uid(norm: str, default_branch: str = "main") -> str:
    return hashlib.sha1(f"{norm}@{default_branch}".encode("utf-8")).hexdigest()[:20]


def test_https_remote_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()
    repo = tmp_path / "r1"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/Org/Repo.git")
    ident = _resolve_project_identity(str(repo))
    assert ident["normalized_remote"] == "github.com/org/repo" or ident["normalized_remote"] == "github.com/Org/Repo".lower()
    assert ident["project_uid"] == _expect_uid("github.com/org/repo")


def test_scp_style_remote_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()
    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    ident = _resolve_project_identity(str(repo))
    assert ident["normalized_remote"] == "github.com/owner/repo"
    assert ident["project_uid"] == _expect_uid("github.com/owner/repo")


def test_ssh_scheme_remote_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()
    repo = tmp_path / "r3"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "ssh://git@github.com/owner/repo.git")
    ident = _resolve_project_identity(str(repo))
    assert ident["normalized_remote"] == "github.com/owner/repo"
    assert ident["project_uid"] == _expect_uid("github.com/owner/repo")

