from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Product, ProductProjectLink, Project


def _seed_backend() -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            p = Project(slug="backend", human_key="Backend")
            session.add(p)
            await session.commit()
            await session.refresh(p)
            assert p.id is not None
            session.add(Agent(project_id=p.id, name="Blue", program="x", model="y", task_description=""))
            await session.commit()
    asyncio.run(_seed())


def _seed_product_cross_project_sender() -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            source = Project(slug="source", human_key="Source")
            target = Project(slug="target", human_key="Target")
            session.add(source)
            session.add(target)
            await session.commit()
            await session.refresh(source)
            await session.refresh(target)
            assert source.id is not None
            assert target.id is not None

            product = Product(product_uid="suite", name="Suite")
            session.add(product)
            await session.commit()
            await session.refresh(product)
            assert product.id is not None

            session.add(ProductProjectLink(product_id=product.id, project_id=source.id))
            session.add(ProductProjectLink(product_id=product.id, project_id=target.id))

            source_sender = Agent(project_id=source.id, name="BlueLake", program="x", model="y", task_description="")
            target_recipient = Agent(project_id=target.id, name="BlueLake", program="x", model="y", task_description="")
            session.add(source_sender)
            session.add(target_recipient)
            await session.commit()
            await session.refresh(source_sender)
            await session.refresh(target_recipient)
            assert source_sender.id is not None
            assert target_recipient.id is not None

            message = Message(
                project_id=target.id,
                sender_id=source_sender.id,
                thread_id="cross-thread",
                subject="Cross Project CLI",
                body_md="Cross project body",
                importance="high",
                ack_required=False,
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)
            assert message.id is not None

            session.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=target_recipient.id,
                    kind="to",
                )
            )
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


def test_cli_products_search_disambiguates_cross_project_sender(isolated_env):
    _seed_product_cross_project_sender()
    runner = CliRunner()
    res = runner.invoke(app, ["products", "search", "Suite", "Cross"])
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout


def test_cli_products_inbox_fallback_disambiguates_cross_project_sender(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        raise RuntimeError("server unavailable")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(app, ["products", "inbox", "Suite", "BlueLake", "--limit", "5"])
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout

