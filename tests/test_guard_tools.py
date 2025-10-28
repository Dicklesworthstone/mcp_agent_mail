from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_install_and_uninstall_precommit_guard_tools(isolated_env, tmp_path: Path):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        # Prepare an empty git repo
        repo_dir = tmp_path / "code"
        repo_dir.mkdir(parents=True, exist_ok=True)
        # Initialize git repo
        import subprocess

        await asyncio.to_thread(subprocess.run, ["git", "init"], cwd=str(repo_dir), check=True)
        await asyncio.to_thread(
            subprocess.run, ["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True
        )
        await asyncio.to_thread(
            subprocess.run, ["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True
        )

        res = await client.call_tool(
            "install_precommit_guard",
            {"project_key": "Backend", "code_repo_path": str(repo_dir)},
        )
        hook_path = Path(res.data.get("hook"))
        assert hook_path.exists()

        res2 = await client.call_tool(
            "uninstall_precommit_guard",
            {"code_repo_path": str(repo_dir)},
        )
        # Tool returns {removed: bool}
        assert bool(res2.data.get("removed")) is True
        assert not hook_path.exists()


