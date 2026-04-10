import asyncio
from pathlib import Path

from mcp_agent_mail.app import _build_project_profile, _compute_project_slug, _resolve_project_identity
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.models import Project
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


def test_identity_dir_mode_preserves_symlink_project_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()

    real_target = tmp_path / "real"
    real_target.mkdir()
    symlink_target = tmp_path / "repo-link"
    symlink_target.symlink_to(real_target, target_is_directory=True)

    real_identity = _resolve_project_identity(str(real_target))
    symlink_identity = _resolve_project_identity(str(symlink_target))

    assert real_identity["slug"] == _compute_project_slug(str(real_target))
    assert symlink_identity["slug"] == _compute_project_slug(str(symlink_target))
    assert symlink_identity["slug"] != real_identity["slug"]
    assert symlink_identity["canonical_path"] == str(symlink_target)
    assert symlink_identity["human_key"] == str(symlink_target)
    assert symlink_identity["project_uid"] != real_identity["project_uid"]


def test_build_project_profile_dedupes_same_file_aliases(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Project Profile\n", encoding="utf-8")
    alias = tmp_path / "readme.md"
    alias.symlink_to(readme)

    profile = asyncio.run(_build_project_profile(Project(slug="proj", human_key=str(tmp_path)), ["BlueLake"]))

    assert profile.count("===== ") == 1
    assert "===== README.md =====" in profile
    assert "# Project Profile" in profile
