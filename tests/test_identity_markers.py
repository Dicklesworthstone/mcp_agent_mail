import hashlib
import subprocess
from pathlib import Path

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def test_committed_marker_precedence(tmp_path: Path, monkeypatch) -> None:
    # Gate on worktrees so identity logic runs in modern mode
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    # Write committed marker
    committed_uid = "deadbeefcafefeed1234"
    (repo / ".agent-mail-project-id").write_text(committed_uid + "\n", encoding="utf-8")
    ident = _resolve_project_identity(str(repo))
    assert ident["project_uid"] == committed_uid


def test_private_marker_used_when_committed_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()

    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    # Compute git-common-dir and write private marker
    gdir = _git(repo, "rev-parse", "--git-common-dir")
    gdir_path = Path(gdir if gdir.startswith("/") else (repo / gdir))
    (gdir_path / "agent-mail").mkdir(parents=True, exist_ok=True)
    private_uid = "00112233445566778899"
    (gdir_path / "agent-mail" / "project-id").write_text(private_uid + "\n", encoding="utf-8")
    ident = _resolve_project_identity(str(repo))
    assert ident["project_uid"] == private_uid


def test_remote_fingerprint_when_no_markers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.delenv("PROJECT_IDENTITY_MODE", raising=False)
    get_settings.cache_clear()

    repo = tmp_path / "repo3"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    # Add a remote URL matching normalized host/owner/repo pattern
    url = "https://github.com/owner/example.git"
    _git(repo, "remote", "add", "origin", url)
    # Default branch resolve may fail; code falls back to 'main'
    expected_default = "main"
    expected_norm = "github.com/owner/example"
    expected_fingerprint = f"{expected_norm}@{expected_default}"
    expected_uid = hashlib.sha1(expected_fingerprint.encode("utf-8")).hexdigest()[:20]
    ident = _resolve_project_identity(str(repo))
    assert ident["project_uid"] == expected_uid

