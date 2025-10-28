from __future__ import annotations

import asyncio
from asyncio.subprocess import PIPE
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
    assert "FILE_RESERVATIONS_DIR" in script and "AGENT_NAME" in script

    # Initialize dummy repo and write a file_reservation artifact that conflicts with the staged file
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0
    # Create a file and stage it
    f = repo_dir / "agents" / "Blue" / "inbox" / "2025" / "10" / "note.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    proc_add = await asyncio.create_subprocess_exec(
        "git",
        "add",
        f.relative_to(repo_dir).as_posix(),
        cwd=str(repo_dir),
    )
    assert (await proc_add.wait()) == 0

    # Write a conflicting file reservation in archive
    reservations_dir = archive.root / "file_reservations"
    reservations_dir.mkdir(parents=True, exist_ok=True)
    (reservations_dir / "c.json").write_text(
        '{"agent":"Other","path_pattern":"agents/*/inbox/*/*/*.md","expires_ts":"2999-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    # Install the guard and run it with AGENT_NAME set to Blue
    hook_path = await install_guard(settings, "backend", repo_dir)
    assert hook_path.exists()
    env = {"AGENT_NAME": "Blue", **{}}
    proc_hook = await asyncio.create_subprocess_exec(
        str(hook_path),
        cwd=str(repo_dir),
        env=env,
        stdout=PIPE,
        stderr=PIPE,
    )
    _stdout_bytes, stderr_bytes = await proc_hook.communicate()
    # Expect non-zero due to conflict and helpful message
    assert proc_hook.returncode != 0
    stderr_text = (stderr_bytes.decode("utf-8", "ignore") if stderr_bytes else "")
    assert "file_reservation" in stderr_text.lower() or "exclusive" in stderr_text.lower()

    # Uninstall guard path returns True and removes file
    removed = await uninstall_guard(repo_dir)
    assert removed is True


