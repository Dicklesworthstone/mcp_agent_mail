from pathlib import Path

from mcp_agent_mail.app import _compute_project_slug, _resolve_project_identity
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.utils import slugify


def test_identity_dir_mode_without_repo(tmp_path: Path, monkeypatch) -> None:
    # Gate off: should behave as strict dir mode
    monkeypatch.setenv("WORKTREES_ENABLED", "0")
    # Ensure defaults
    monkeypatch.delenv("PROJECT_IDENTITY_MODE", raising=False)
    get_settings.cache_clear()

    target = tmp_path / "proj"
    target.mkdir(parents=True, exist_ok=True)
    ident = _resolve_project_identity(str(target))
    # Mode should be dir and slug should match _compute_project_slug for the path
    assert ident["identity_mode_used"] == "dir"
    assert ident["slug"] == _compute_project_slug(str(target))
    # Fallback slugify should also equal compute when gate is off
    assert ident["slug"] == slugify(str(target))


def test_identity_mode_git_common_dir_without_repo_falls_back(tmp_path: Path, monkeypatch) -> None:
    # Gate on, but no repo: should fall back to dir behavior for canonical path and slug
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "git-common-dir")
    get_settings.cache_clear()

    target = tmp_path / "proj2"
    target.mkdir(parents=True, exist_ok=True)
    ident = _resolve_project_identity(str(target))
    # With no repo, canonical path is the target path, and slug uses dir fallback
    assert Path(ident["canonical_path"]).resolve() == target.resolve()
    assert ident["slug"] == _compute_project_slug(str(target))

