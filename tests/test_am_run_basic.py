import asyncio
import hashlib
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import click
import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.cli import (
    _build_slot_renew_interval_seconds,
    _effective_build_slot_ttl_seconds,
    am_run,
    app,
)
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project

runner = CliRunner()


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


def test_am_run_blocks_on_existing_exclusive_conflicts_even_for_shared_request(tmp_path: Path, monkeypatch) -> None:
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
                                    "exclusive": True,
                                    "expires_ts": "2026-04-10T03:00:00Z",
                                }
                            ]
                        }
                    },
                }
            )
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when an existing exclusive holder conflicts")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.exceptions.Exit) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=True,
            block_on_conflicts=True,
        )

    assert excinfo.value.exit_code == 1


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


def test_am_run_uses_repo_root_identity_for_subdirectory_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "0")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    repo = tmp_path / "repo"
    subdir = repo / "nested" / "work"
    subdir.mkdir(parents=True, exist_ok=True)

    import subprocess as stdlib_subprocess

    stdlib_subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    stdlib_subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    stdlib_subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)

    root_ident = _resolve_project_identity(str(repo))
    subdir_ident = _resolve_project_identity(str(subdir))
    assert root_ident["slug"] != subdir_ident["slug"]

    captured: dict[str, Any] = {}

    class _CompletedProcess:
        returncode = 0

    def fake_run(cmd, env=None, check=False, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env or {})
        return _CompletedProcess()

    monkeypatch.setattr("subprocess.run", fake_run)

    am_run(
        slot="subdir-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=subdir,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )

    assert captured["env"]["SLUG"] == root_ident["slug"]
    assert captured["env"]["PROJECT_UID"] == root_ident["project_uid"]
    expected_artifact_dir = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / root_ident["slug"]
        / "artifacts"
        / "TestAgent"
        / captured["env"]["BRANCH"]
    )
    assert captured["env"]["ARTIFACT_DIR"] == str(expected_artifact_dir)


def test_am_run_local_fallback_normalizes_short_ttl_before_running_child(tmp_path: Path, monkeypatch) -> None:
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

    class _CompletedProcess:
        returncode = 0

    def fake_run(cmd, env=None, check=False, **kwargs):
        lease_files = list(
            Path(get_settings().storage.root).expanduser().resolve().glob(
                "projects/*/build_slots/unittest-slot/*.json"
            )
        )
        assert len(lease_files) == 1
        payload = json.loads(lease_files[0].read_text(encoding="utf-8"))
        acquired = datetime.fromisoformat(payload["acquired_ts"])
        expires = datetime.fromisoformat(payload["expires_ts"])
        assert (expires - acquired).total_seconds() >= 60
        return _CompletedProcess()

    monkeypatch.setattr("subprocess.run", fake_run)

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=30,
        shared=False,
    )


def test_am_run_local_fallback_does_not_shorten_active_same_holder_lease(tmp_path: Path, monkeypatch) -> None:
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
    ident = _resolve_project_identity(str(proj))
    lease_path = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / ident["slug"]
        / "build_slots"
        / "unittest-slot"
        / "TestAgent__unknown.json"
    )
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    original_acquired = "2026-04-10T01:00:00+00:00"
    original_exp = "2099-04-10T02:00:00+00:00"
    lease_path.write_text(
        json.dumps(
            {
                "slot": "unittest-slot",
                "agent": "TestAgent",
                "branch": "unknown",
                "exclusive": True,
                "acquired_ts": original_acquired,
                "expires_ts": original_exp,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    class _CompletedProcess:
        returncode = 0

    def fake_run(cmd, env=None, check=False, **kwargs):
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        assert payload["acquired_ts"] == original_acquired
        assert datetime.fromisoformat(payload["expires_ts"]) >= datetime.fromisoformat(original_exp)
        return _CompletedProcess()

    monkeypatch.setattr("subprocess.run", fake_run)

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=30,
        shared=False,
    )


def test_am_run_local_fallback_blocks_on_existing_exclusive_conflicts_even_for_shared_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "httpx.Client.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("server unavailable")),
    )

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    ident = _resolve_project_identity(str(proj))
    slot_dir = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / ident["slug"]
        / "build_slots"
        / "unittest-slot"
    )
    slot_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir / "OtherAgent__main.json").write_text(
        json.dumps(
            {
                "slot": "unittest-slot",
                "agent": "OtherAgent",
                "branch": "main",
                "exclusive": True,
                "acquired_ts": "2026-04-10T01:00:00+00:00",
                "expires_ts": "2099-04-10T02:00:00+00:00",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when a local exclusive holder conflicts")

    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.exceptions.Exit) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=True,
            block_on_conflicts=True,
        )

    assert excinfo.value.exit_code == 1


def test_build_slot_renew_timing_uses_half_life_of_effective_ttl() -> None:
    assert _effective_build_slot_ttl_seconds(30) == 60
    assert _build_slot_renew_interval_seconds(30) == 30
    assert _build_slot_renew_interval_seconds(120) == 60


def test_am_run_server_uses_effective_ttl_for_build_slot_requests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    acquire_ttls: list[int] = []

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        arguments = ((json or {}).get("params") or {}).get("arguments") or {}
        if tool_name == "acquire_build_slot":
            acquire_ttls.append(int(arguments["ttl_seconds"]))
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
        ttl_seconds=30,
        shared=False,
    )

    assert acquire_ttls == [60]


def test_am_run_stops_server_renewer_before_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    release_started = threading.Event()
    renewed_during_release = threading.Event()

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        if tool_name == "release_build_slot":
            release_started.set()
            time.sleep(0.05)
        elif tool_name == "renew_build_slot" and release_started.is_set():
            renewed_during_release.set()
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    class _CompletedProcess:
        returncode = 0

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _CompletedProcess())
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _: 0.01)

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )

    assert release_started.is_set()
    assert not renewed_during_release.is_set()


def test_am_run_server_requests_include_stable_branch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    _seed_project_agent(proj, "TestAgent", "secret-token")

    seen_branches: list[tuple[str, str | None]] = []

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        arguments = ((json or {}).get("params") or {}).get("arguments") or {}
        if tool_name in {"acquire_build_slot", "renew_build_slot", "release_build_slot"}:
            seen_branches.append((str(tool_name), arguments.get("branch")))
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    class _CompletedProcess:
        returncode = 0

    def fake_run(*args, **kwargs):
        time.sleep(0.05)
        return _CompletedProcess()

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _: 0.01)

    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )

    assert ("acquire_build_slot", "unknown") in seen_branches
    assert ("release_build_slot", "unknown") in seen_branches
    assert any(tool_name == "renew_build_slot" and branch == "unknown" for tool_name, branch in seen_branches)


def test_amctl_env_emits_shell_safe_key_value_lines_for_long_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "0")
    monkeypatch.setenv("COLUMNS", "40")
    get_settings.cache_clear()

    repo = tmp_path / "repo-with-a-very-long-name-for-env-output"
    subdir = repo / "nested" / "path" / "for" / "env"
    subdir.mkdir(parents=True, exist_ok=True)

    import subprocess as stdlib_subprocess

    stdlib_subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    stdlib_subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    stdlib_subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)

    root_ident = _resolve_project_identity(str(repo))
    result = runner.invoke(app, ["amctl", "env", "--path", str(subdir), "--agent", "TestAgent"])

    assert result.exit_code == 0
    env_lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env_lines["SLUG"] == root_ident["slug"]
    assert env_lines["PROJECT_UID"] == root_ident["project_uid"]
    assert env_lines["AGENT"] == "TestAgent"
    expected_artifact_dir = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / root_ident["slug"]
        / "artifacts"
        / "TestAgent"
        / env_lines["BRANCH"]
    )
    assert env_lines["ARTIFACT_DIR"] == str(expected_artifact_dir)
