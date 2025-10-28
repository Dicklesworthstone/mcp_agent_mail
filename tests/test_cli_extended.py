from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project


def _seed_backend() -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            p = Project(slug="backend", human_key="Backend")
            session.add(p)
            await session.commit()
            await session.refresh(p)
            session.add(Agent(project_id=p.id, name="Blue", program="x", model="y", task_description=""))
            await session.commit()
    asyncio.run(_seed())


def test_cli_file_reservations_list_and_active(tmp_path: Path, isolated_env):
    _seed_backend()
    runner = CliRunner()
    # file_reservations list (no reservations yet)
    res = runner.invoke(app, ["file_reservations", "list", "Backend"])  # just ensure it runs
    assert res.exit_code == 0
    # active view
    res2 = runner.invoke(app, ["file_reservations", "active", "Backend", "--limit", "5"])  # runs even when empty
    assert res2.exit_code == 0


def test_cli_acks_pending_and_overdue(isolated_env):
    _seed_backend()
    runner = CliRunner()
    # pending acks for Blue (empty)
    res = runner.invoke(app, ["acks", "pending", "Backend", "Blue", "--limit", "5"])
    assert res.exit_code == 0
    # overdue (empty)
    res2 = runner.invoke(app, ["acks", "overdue", "Backend", "Blue", "--ttl-minutes", "60", "--limit", "10"])
    assert res2.exit_code == 0


def test_cli_guard_install_uninstall(tmp_path: Path, isolated_env):
    _seed_backend()
    # init a git repo
    repo_dir = tmp_path / "r"
    repo_dir.mkdir(parents=True, exist_ok=True)
    from subprocess import run
    run(["git", "init"], cwd=str(repo_dir), check=True)
    run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
    run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True)

    runner = CliRunner()
    # install
    res = runner.invoke(app, ["guard", "install", "Backend", str(repo_dir)])
    assert res.exit_code == 0
    # uninstall
    res2 = runner.invoke(app, ["guard", "uninstall", str(repo_dir)])
    assert res2.exit_code == 0


def test_cli_list_projects_and_serve_http_overrides(isolated_env, monkeypatch):
    _seed_backend()
    runner = CliRunner()
    # list-projects should print table
    res = runner.invoke(app, ["list-projects", "--include-agents"])  # smoke
    assert res.exit_code == 0
    # serve-http should honor host/port/path overrides and not crash (monkeypatch uvicorn)
    calls: dict[str, object] = {}
    def fake_uvicorn_run(app, host, port, log_level="info"):
        calls["host"] = host
        calls["port"] = port
        calls["log_level"] = log_level
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    res2 = runner.invoke(app, ["serve-http", "--host", "0.0.0.0", "--port", "9999", "--path", "/m"])
    assert res2.exit_code == 0
    assert calls.get("host") == "0.0.0.0"
    assert calls.get("port") == 9999


