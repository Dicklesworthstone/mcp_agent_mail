import subprocess
from pathlib import Path

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def test_clones_share_same_project_uid_via_remote(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()
    # Create bare remote
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    # Clone twice
    c1 = tmp_path / "clone1"
    c2 = tmp_path / "clone2"
    _git(tmp_path, "clone", str(remote), str(c1))
    _git(tmp_path, "clone", str(remote), str(c2))
    id1 = _resolve_project_identity(str(c1))
    id2 = _resolve_project_identity(str(c2))
    assert id1["normalized_remote"] == id2["normalized_remote"]
    assert id1["project_uid"] == id2["project_uid"]

