from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import install_guard, render_precommit_script, uninstall_guard
from mcp_agent_mail.storage import ensure_archive


@pytest.mark.asyncio
async def test_guard_render_and_conflict_message(isolated_env, tmp_path: Path):
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script = render_precommit_script(archive)
    assert "CLAIMS_DIR" in script and "AGENT_NAME" in script

    # Initialize dummy repo and write a claim file that conflicts with staged file
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True)
    # Create a file and stage it
    f = repo_dir / "agents" / "Blue" / "inbox" / "2025" / "10" / "note.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", f.relative_to(repo_dir).as_posix()], cwd=str(repo_dir), check=True)

    # Write a conflicting claim in archive
    claims_dir = archive.root / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "c.json").write_text(
        '{"agent":"Other","path_pattern":"agents/*/inbox/*/*/*.md","expires_ts":"2999-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    # Install the guard and run it with AGENT_NAME set to Blue
    hook_path = await install_guard(settings, "backend", repo_dir)
    assert hook_path.exists()
    env = {"AGENT_NAME": "Blue", **dict()}
    result = subprocess.run([str(hook_path)], cwd=str(repo_dir), env=env, capture_output=True, text=True)
    # Expect non-zero due to conflict and helpful message
    assert result.returncode != 0
    assert "Exclusive claim conflicts" in (result.stderr or "") or "exclusive claim" in (result.stderr or "").lower()

    # Uninstall guard path returns True and removes file
    removed = await uninstall_guard(repo_dir)
    assert removed is True


