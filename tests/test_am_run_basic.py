import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any, cast

import click
import httpx
import pytest
from sqlalchemy import select

from mcp_agent_mail.cli import am_run
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project


def test_am_run_creates_lease_when_enabled(tmp_path: Path, monkeypatch) -> None:
    # Point archive to a temp root and enable worktrees features
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "warn")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "httpx.Client.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("server unavailable")),
    )

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    # Run a trivial child that exits 0
    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )
    # Confirm lease was created under archive build_slots
    archive_root = Path(get_settings().storage.root).expanduser().resolve()
    # We don't know the slug in advance; scan for build_slots presence
    projects_dir = archive_root / "projects"
    assert projects_dir.exists()
    # At least one project directory should have a build_slots/unittest-slot/ file inside
    found = False
    for entry in projects_dir.glob("*/build_slots/unittest-slot/*.json"):
        if entry.is_file():
            found = True
            break
    assert found, "Expected a lease JSON file to be created for am-run"


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


def _seed_project_agent(project_path: Path, agent_name: str, token: str) -> None:
    project_key = str(project_path.resolve())
    slug = f"proj-{hashlib.sha256(project_key.encode('utf-8')).hexdigest()[:12]}"

    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            existing = await session.execute(select(Project).where(cast(Any, Project.human_key == project_key)))
            project = existing.scalars().first()
            if project is None:
                project = Project(slug=slug, human_key=project_key)
                session.add(project)
                await session.commit()
                await session.refresh(project)
            assert project.id is not None
            existing_agent = await session.execute(
                select(Agent).where(
                    cast(Any, Agent.project_id == project.id),
                    cast(Any, Agent.name == agent_name),
                )
            )
            agent = existing_agent.scalars().first()
            if agent is None:
                session.add(
                    Agent(
                        project_id=project.id,
                        name=agent_name,
                        program="x",
                        model="y",
                        task_description="",
                        registration_token=token,
                    )
                )
            else:
                agent.registration_token = token
            await session.commit()

    asyncio.run(_seed())


def test_am_run_blocks_on_structured_content_conflicts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        if tool_name == "acquire_build_slot":
            return _StaticJsonResponse(
                {
                    "jsonrpc": "2.0",
                    "id": "am-run-acquire",
                    "result": {
                        "structuredContent": {
                            "conflicts": [
                                {
                                    "slot": "unittest-slot",
                                    "agent": "OtherAgent",
                                    "branch": "main",
                                    "expires_ts": "2026-04-10T03:00:00Z",
                                }
                            ]
                        }
                    },
                }
            )
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when server reports a build-slot conflict")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.exceptions.Exit) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=False,
            block_on_conflicts=True,
        )

    assert excinfo.value.exit_code == 1
    archive_root = Path(get_settings().storage.root).expanduser().resolve()
    active_leases = list(archive_root.glob("projects/*/build_slots/unittest-slot/*.json"))
    assert active_leases == []


def test_am_run_surfaces_server_error_without_local_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")
    seen_tools: list[str] = []

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        seen_tools.append(str(tool_name))
        if tool_name == "acquire_build_slot":
            return _StaticJsonResponse(
                {
                    "jsonrpc": "2.0",
                    "id": "am-run-acquire",
                    "error": {"message": "server denied build slot"},
                }
            )
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when server rejects build-slot acquisition")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.ClickException) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=False,
            block_on_conflicts=True,
        )

    assert "server denied build slot" in str(excinfo.value)
    assert "release_build_slot" not in seen_tools


def test_am_run_includes_registration_token_in_server_requests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    seen_tokens: list[tuple[str, str | None]] = []

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        arguments = ((json or {}).get("params") or {}).get("arguments") or {}
        if tool_name in {"acquire_build_slot", "release_build_slot"}:
            seen_tokens.append((tool_name, arguments.get("registration_token")))
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    class _CompletedProcess:
        returncode = 0

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _CompletedProcess())

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )

    assert ("acquire_build_slot", "secret-token") in seen_tokens
    assert ("release_build_slot", "secret-token") in seen_tokens


def test_am_run_resolves_registration_token_case_insensitively(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "testagent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    seen_tokens: list[tuple[str, str | None]] = []

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        arguments = ((json or {}).get("params") or {}).get("arguments") or {}
        if tool_name in {"acquire_build_slot", "release_build_slot"}:
            seen_tokens.append((tool_name, arguments.get("registration_token")))
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    class _CompletedProcess:
        returncode = 0

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _CompletedProcess())

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="testagent",
        ttl_seconds=120,
        shared=False,
    )

    assert ("acquire_build_slot", "secret-token") in seen_tokens
    assert ("release_build_slot", "secret-token") in seen_tokens


def test_am_run_surfaces_invalid_json_without_local_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    def fake_post(self, url, json=None, headers=None):
        return _InvalidJsonResponse()

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when server returns invalid JSON")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.ClickException) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=False,
            block_on_conflicts=True,
        )

    assert "invalid JSON response from server" in str(excinfo.value)


def test_am_run_surfaces_http_status_without_local_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    def fake_post(self, url, json=None, headers=None):
        return _StatusErrorResponse(401)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when server returns an HTTP status error")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.ClickException) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=False,
            block_on_conflicts=True,
        )

    assert "HTTP 401 from server" in str(excinfo.value)
