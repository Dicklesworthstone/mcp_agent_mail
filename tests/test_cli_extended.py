from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Product, ProductProjectLink, Project


class _StaticJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _InvalidJsonResponse:
    def json(self):
        raise ValueError("invalid json")

    def raise_for_status(self):
        return None


class _StatusErrorResponse:
    def __init__(self, status_code: int):
        request = httpx.Request("POST", "http://testserver/mcp")
        self._response = httpx.Response(status_code, request=request)

    def json(self):
        raise AssertionError("json() should not be called after an HTTP status error")

    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            f"HTTP {self._response.status_code}",
            request=self._response.request,
            response=self._response,
        )


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

            source_sender = Agent(
                project_id=source.id,
                name="BlueLake",
                program="x",
                model="y",
                task_description="",
                registration_token="shared-token",
            )
            target_recipient = Agent(
                project_id=target.id,
                name="BlueLake",
                program="x",
                model="y",
                task_description="",
                registration_token="shared-token",
            )
            private_source_sender = Agent(
                project_id=source.id,
                name="RedStone",
                program="x",
                model="y",
                task_description="",
                registration_token="private-token",
            )
            private_target_recipient = Agent(
                project_id=target.id,
                name="RedStone",
                program="x",
                model="y",
                task_description="",
                registration_token="private-token",
            )
            session.add(source_sender)
            session.add(target_recipient)
            session.add(private_source_sender)
            session.add(private_target_recipient)
            await session.commit()
            await session.refresh(source_sender)
            await session.refresh(target_recipient)
            await session.refresh(private_source_sender)
            await session.refresh(private_target_recipient)
            assert source_sender.id is not None
            assert target_recipient.id is not None
            assert private_source_sender.id is not None
            assert private_target_recipient.id is not None

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
            private_message = Message(
                project_id=target.id,
                sender_id=private_source_sender.id,
                thread_id="private-thread",
                subject="Private Cross Message",
                body_md="Private cross project body",
                importance="normal",
                ack_required=False,
            )
            session.add(private_message)
            await session.commit()
            await session.refresh(private_message)
            assert private_message.id is not None
            session.add(
                MessageRecipient(
                    message_id=private_message.id,
                    agent_id=private_target_recipient.id,
                    kind="to",
                )
            )
            await session.commit()

    asyncio.run(_seed())


def _seed_product_with_relative_project(project_dir: Path, *, human_key: Path | None = None) -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            product = Product(product_uid="suite", name="Suite")
            stored_human_key = str(human_key) if human_key is not None else str(project_dir.resolve())
            project = Project(slug="relative-project", human_key=stored_human_key)
            session.add(product)
            session.add(project)
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


def test_cli_acks_pending_resolves_agent_case_insensitively(isolated_env):
    _seed_backend()
    runner = CliRunner()
    res = runner.invoke(app, ["acks", "pending", "Backend", "blue", "--limit", "5"])
    assert res.exit_code == 0


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
        calls["app"] = app
        calls["host"] = host
        calls["port"] = port
        calls["log_level"] = log_level

    def fake_build_http_app(settings, server):
        calls["path"] = settings.http.path
        return object()

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    monkeypatch.setattr("mcp_agent_mail.cli.build_http_app", fake_build_http_app)
    monkeypatch.setattr("mcp_agent_mail.cli.build_mcp_server", lambda: object())
    res2 = runner.invoke(app, ["serve-http", "--host", "0.0.0.0", "--port", "9999", "--path", "/m"])
    assert res2.exit_code == 0
    assert calls.get("path") == "/m"
    assert calls.get("host") == "0.0.0.0"
    assert calls.get("port") == 9999


def test_cli_products_search_disambiguates_cross_project_sender(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        raise httpx.ConnectError("server unavailable")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "search", "Suite", "Cross", "--agent", "BlueLake", "--registration-token", "shared-token"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout
    assert "Private Cross Message" not in res.stdout


def test_cli_products_search_falls_back_when_fts_query_fails(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    from sqlalchemy.ext.asyncio import AsyncSession

    original_execute = AsyncSession.execute

    async def flaky_execute(self, statement, *args, **kwargs):
        if "fts_messages" in str(statement):
            raise RuntimeError("fts unavailable")
        return await original_execute(self, statement, *args, **kwargs)

    def fake_post(self, *args, **kwargs):
        raise httpx.ConnectError("server unavailable")

    monkeypatch.setattr(AsyncSession, "execute", flaky_execute)
    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "search", "Suite", "Cross", "--agent", "BlueLake", "--registration-token", "shared-token"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout
    assert "Private Cross Message" not in res.stdout


def test_cli_products_inbox_fallback_disambiguates_cross_project_sender(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        raise httpx.ConnectError("server unavailable")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "BlueLake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout


def test_cli_products_link_resolves_relative_project_path(tmp_path: Path, isolated_env, monkeypatch):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    _seed_product_with_relative_project(project_dir)

    runner = CliRunner()
    res = runner.invoke(app, ["products", "link", "Suite", "."])

    assert res.exit_code == 0
    assert "relative-project" in res.stdout


def test_cli_products_link_resolves_relative_symlink_project_path(tmp_path: Path, isolated_env, monkeypatch):
    real_project_dir = tmp_path / "real-repo"
    real_project_dir.mkdir()
    symlink_project_dir = tmp_path / "repo-link"
    symlink_project_dir.symlink_to(real_project_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    _seed_product_with_relative_project(real_project_dir, human_key=symlink_project_dir)

    runner = CliRunner()
    res = runner.invoke(app, ["products", "link", "Suite", "./repo-link"])

    assert res.exit_code == 0
    assert "relative-project" in res.stdout


def test_cli_products_inbox_fallback_resolves_agent_case_insensitively(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        raise httpx.ConnectError("server unavailable")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "bluelake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout


def test_cli_products_ensure_reads_structured_content_response(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-ensure",
                "result": {
                    "structuredContent": {
                        "id": 99,
                        "product_uid": "suite",
                        "name": "Suite",
                        "created_at": "2026-04-10T02:35:00Z",
                    }
                },
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(app, ["products", "ensure", "suite", "--name", "Suite"])
    assert res.exit_code == 0
    assert "99" in res.stdout
    assert "Suite" in res.stdout


def test_cli_products_ensure_surfaces_server_error_without_local_fallback(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-ensure",
                "error": {"message": "permission denied"},
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(app, ["products", "ensure", "suite", "--name", "Suite"])
    assert res.exit_code == 1
    assert "Product" not in res.stdout


def test_cli_products_ensure_surfaces_invalid_json_without_local_fallback(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _InvalidJsonResponse()

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(app, ["products", "ensure", "suite", "--name", "Suite"])
    assert res.exit_code == 1
    assert "Product" not in res.stdout


def test_cli_products_ensure_surfaces_http_status_without_local_fallback(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StatusErrorResponse(401)

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(app, ["products", "ensure", "suite", "--name", "Suite"])
    assert res.exit_code == 1
    assert "HTTP 401 from server" in res.output
    assert "Product" not in res.stdout


def test_cli_products_inbox_reads_structured_content_response(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-inbox",
                "result": {
                    "structuredContent": {
                        "result": [
                            {
                                "project_id": 2,
                                "id": 7,
                                "subject": "Server Inbox Message",
                                "from": "BlueLake@source",
                                "importance": "high",
                                "created_ts": "2026-04-10T02:35:00Z",
                            }
                        ]
                    }
                },
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "BlueLake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout
    assert "No messages found." not in res.stdout


def test_cli_products_inbox_reads_list_shaped_structured_content_response(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-inbox",
                "result": {
                    "structuredContent": [
                        {
                            "project_id": 2,
                            "id": 7,
                            "subject": "Server Inbox Message",
                            "from": "BlueLake@source",
                            "importance": "high",
                            "created_ts": "2026-04-10T02:35:00Z",
                        }
                    ]
                },
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "BlueLake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 0
    assert "BlueLake@source" in res.stdout
    assert "No messages found." not in res.stdout


def test_cli_products_inbox_does_not_fallback_when_server_returns_empty_result(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-inbox",
                "result": {"structuredContent": {"result": []}},
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "BlueLake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 0
    assert "No messages found." in res.stdout
    assert "BlueLake@source" not in res.stdout


def test_cli_products_inbox_surfaces_server_error_without_local_fallback(isolated_env, monkeypatch):
    _seed_product_cross_project_sender()
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-inbox",
                "error": {"message": "forbidden"},
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        ["products", "inbox", "Suite", "BlueLake", "--registration-token", "shared-token", "--limit", "5"],
    )
    assert res.exit_code == 1
    assert "BlueLake@source" not in res.stdout


def test_cli_products_summarize_thread_reads_structured_content_response(isolated_env, monkeypatch):
    runner = CliRunner()

    def fake_post(self, *args, **kwargs):
        return _StaticJsonResponse(
            {
                "jsonrpc": "2.0",
                "id": "cli-products-summarize-thread",
                "result": {
                    "structuredContent": {
                        "result": {
                            "summary": {
                                "participants": ["BlueLake@source", "BlueLake"],
                                "total_messages": 1,
                                "open_actions": 0,
                                "done_actions": 1,
                                "key_points": ["Server summary point"],
                                "action_items": ["Follow up"],
                            },
                            "examples": [
                                {
                                    "id": 7,
                                    "subject": "Server Thread Message",
                                    "from": "BlueLake@source",
                                    "created_ts": "2026-04-10T02:35:00Z",
                                }
                            ],
                        }
                    }
                },
            }
        )

    monkeypatch.setattr("httpx.Client.post", fake_post)
    res = runner.invoke(
        app,
        [
            "products",
            "summarize-thread",
            "Suite",
            "cross-thread",
            "--agent",
            "BlueLake",
            "--registration-token",
            "shared-token",
            "--no-llm",
        ],
    )
    assert res.exit_code == 0
    assert "Server summary point" in res.stdout
    assert "BlueLake@source" in res.stdout
