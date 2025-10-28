from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import render_precommit_script
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Configure dummy user to avoid git warnings
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def _stage_file(repo_path: Path, rel_path: str, content: str = "x") -> None:
    target = repo_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=str(repo_path), check=True)


def _run_precommit(script_path: Path, repo_path: Path, agent_name: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["AGENT_NAME"] = agent_name
    return subprocess.run(["python", str(script_path)], cwd=str(repo_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.asyncio
async def test_precommit_no_conflict(isolated_env, tmp_path: Path):
    settings = get_settings()
    # Prepare project archive and render guard script
    archive = await ensure_archive(settings, "backend")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Create a separate code repo and stage a file
    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    # No file reservations present -> should pass
    proc = _run_precommit(script_path, code_repo, agent_name="Alpha")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_precommit_conflict_detected(isolated_env, tmp_path: Path):
    settings = get_settings()
    # Prepare project archive and render guard script
    archive = await ensure_archive(settings, "backend")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write an active file reservation held by another agent
    await write_file_reservation_record(
        archive,
        {
            "agent": "Beta",
            "path_pattern": "src/app.py",
            # no expires_ts means treated as active by the guard script
        },
    )

    # Create a separate code repo and stage a file matching the reservation
    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    # AGENT_NAME is Alpha; reservation is held by Beta -> should block
    proc = _run_precommit(script_path, code_repo, agent_name="Alpha")
    assert proc.returncode == 1
    assert "Exclusive file_reservation conflicts detected" in (proc.stderr or "")
