import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.sql import ColumnElement
from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, FileReservation, Project


def test_cli_lint(monkeypatch):
    runner = CliRunner()
    captured: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        captured.append(command)

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0
    assert captured == [["ruff", "check", "--fix", "--unsafe-fixes"]]


def test_cli_typecheck(monkeypatch):
    runner = CliRunner()
    captured: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        captured.append(command)

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    result = runner.invoke(app, ["typecheck"])
    assert result.exit_code == 0
    assert captured == [["uvx", "ty", "check"]]


def test_cli_serve_http_uses_settings(isolated_env, monkeypatch):
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_uvicorn_run(app, host, port, log_level="info"):
        call_args["app"] = app
        call_args["host"] = host
        call_args["port"] = port
        call_args["log_level"] = log_level

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    result = runner.invoke(app, ["serve-http"])
    assert result.exit_code == 0
    assert call_args["host"] == "127.0.0.1"
    assert call_args["port"] == 8765


def test_cli_config_set_port_clears_cached_settings(tmp_path, monkeypatch):
    runner = CliRunner()
    env_path = tmp_path / ".env"
    env_path.write_text("HTTP_HOST=127.0.0.1\nHTTP_PORT=1111\nHTTP_PATH=/api/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HTTP_HOST", raising=False)
    monkeypatch.delenv("HTTP_PORT", raising=False)
    monkeypatch.delenv("HTTP_PATH", raising=False)
    clear_settings_cache()

    show_before = runner.invoke(app, ["config", "show-port"])
    assert show_before.exit_code == 0
    assert "1111" in show_before.stdout

    set_result = runner.invoke(app, ["config", "set-port", "2222"])
    assert set_result.exit_code == 0

    show_after = runner.invoke(app, ["config", "show-port"])
    assert show_after.exit_code == 0
    assert "2222" in show_after.stdout


def test_cli_serve_stdio(isolated_env, monkeypatch):
    """Test that serve-stdio invokes FastMCP.run with stdio transport."""
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_run(self, transport="stdio", **kwargs):
        call_args["transport"] = transport
        call_args["kwargs"] = kwargs

    # Patch FastMCP.run on the class before build_mcp_server returns an instance
    from fastmcp import FastMCP

    monkeypatch.setattr(FastMCP, "run", fake_run)
    result = runner.invoke(app, ["serve-stdio"])
    assert result.exit_code == 0
    assert call_args["transport"] == "stdio"


def test_cli_migrate(monkeypatch):
    runner = CliRunner()
    invoked: dict[str, bool] = {"called": False}

    async def fake_migrate(settings):
        invoked["called"] = True

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", fake_migrate)
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0
    assert invoked["called"] is True


def test_cli_list_projects(isolated_env):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="demo", human_key="Demo")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None
            session.add(
                Agent(
                    project_id=project.id,
                    name="BlueLake",
                    program="codex",
                    model="gpt-5",
                    task_description="",
                )
            )
            await session.commit()

    asyncio.run(seed())
    result = runner.invoke(app, ["list-projects", "--include-agents"])
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "BlueLake" not in result.stdout


def test_cli_list_projects_json_returns_structured_error_on_failure(monkeypatch):
    runner = CliRunner()

    async def failing_ensure_schema(_settings=None) -> None:
        raise RuntimeError("projects exploded")

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", failing_ensure_schema)

    result = runner.invoke(app, ["list-projects", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "projects exploded"}


def test_archive_save_defaults_to_archive_preset(tmp_path, isolated_env, monkeypatch):
    runner = CliRunner()
    archive_path = tmp_path / "state.zip"
    archive_path.write_bytes(b"zip")
    captured: dict[str, Any] = {}

    def fake_archive(**kwargs):
        captured.update(kwargs)
        metadata = {"scrub_preset": kwargs["scrub_preset"], "projects_requested": list(kwargs["project_filters"])}
        return archive_path, metadata

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_archive)
    result = runner.invoke(app, ["archive", "save"])
    assert result.exit_code == 0
    assert captured["scrub_preset"] == "archive"


def test_clear_and_reset_skips_archive_when_disabled(isolated_env, monkeypatch):
    runner = CliRunner()

    def _should_not_run(**_kwargs):  # pragma: no cover - defensive
        raise AssertionError("archive should not be invoked when --no-archive is supplied")

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", _should_not_run)
    result = runner.invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 0


def test_doctor_check_reports_stale_locks(isolated_env):
    runner = CliRunner()
    settings = get_settings()
    lock_path = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    metadata_path = lock_path.parent / ".archive.lock.owner.json"
    metadata_path.write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    locks_diag = next(item for item in payload["diagnostics"] if item["name"] == "Locks")
    assert locks_diag["status"] == "warning"
    assert "stale" in locks_diag["message"].lower()


def test_doctor_check_detects_non_sqlite3_wal_files(tmp_path, monkeypatch):
    runner = CliRunner()
    db_path = tmp_path / "mail.db"
    wal_path = tmp_path / "mail.db-wal"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    clear_settings_cache()

    sqlite3.connect(db_path).close()
    wal_path.write_text("wal", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    wal_diag = next(item for item in payload["diagnostics"] if item["name"] == "WAL Files")
    assert wal_diag["status"] == "info"
    assert "wal/shm file" in wal_diag["message"].lower()


def test_doctor_check_scopes_project_specific_findings(isolated_env):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key="/backend")
            frontend = Project(slug="frontend", human_key="/frontend")
            session.add(backend)
            session.add(frontend)
            await session.commit()
            await session.refresh(backend)
            await session.refresh(frontend)
            assert backend.id is not None
            assert frontend.id is not None

            backend_agent = Agent(project_id=backend.id, name="BlueLake", program="codex", model="gpt-5", task_description="")
            frontend_agent = Agent(project_id=frontend.id, name="GreenCastle", program="codex", model="gpt-5", task_description="")
            session.add(backend_agent)
            session.add(frontend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            await session.refresh(frontend_agent)
            assert backend_agent.id is not None
            assert frontend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            session.add(
                FileReservation(
                    project_id=frontend.id,
                    agent_id=frontend_agent.id,
                    path_pattern="src/frontend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    asyncio.run(seed())

    settings = get_settings()
    for slug in ("backend", "frontend"):
        lock_path = Path(settings.storage.root) / "projects" / slug / ".archive.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
        (lock_path.parent / ".archive.lock.owner.json").write_text(
            json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
            encoding="utf-8",
        )

    result = runner.invoke(app, ["doctor", "check", "Backend", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    locks_diag = next(item for item in payload["diagnostics"] if item["name"] == "Locks")
    reservations_diag = next(item for item in payload["diagnostics"] if item["name"] == "File Reservations")
    assert "1 stale lock" in locks_diag["message"].lower()
    assert "1 expired reservation" in reservations_diag["message"].lower()


def test_doctor_backups_json_returns_structured_error_on_failure(monkeypatch):
    runner = CliRunner()

    async def failing_list_backups(_settings) -> list[dict[str, Any]]:
        raise RuntimeError("backup listing exploded")

    monkeypatch.setattr("mcp_agent_mail.storage.list_backups", failing_list_backups)

    result = runner.invoke(app, ["doctor", "backups", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "backup listing exploded"}


def test_doctor_repair_scopes_project_specific_repairs(isolated_env, monkeypatch):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key="/backend")
            frontend = Project(slug="frontend", human_key="/frontend")
            session.add(backend)
            session.add(frontend)
            await session.commit()
            await session.refresh(backend)
            await session.refresh(frontend)
            assert backend.id is not None
            assert frontend.id is not None

            backend_agent = Agent(project_id=backend.id, name="BlueLake", program="codex", model="gpt-5", task_description="")
            frontend_agent = Agent(project_id=frontend.id, name="GreenCastle", program="codex", model="gpt-5", task_description="")
            session.add(backend_agent)
            session.add(frontend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            await session.refresh(frontend_agent)
            assert backend_agent.id is not None
            assert frontend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            session.add(
                FileReservation(
                    project_id=frontend.id,
                    agent_id=frontend_agent.id,
                    path_pattern="src/frontend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    async def fake_backup(*args, **kwargs):
        return Path("/tmp/fake-doctor-backup")

    asyncio.run(seed())
    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_backup)

    settings = get_settings()
    backend_lock = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    backend_lock.parent.mkdir(parents=True, exist_ok=True)
    backend_lock.write_text("", encoding="utf-8")
    (backend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )
    frontend_lock = Path(settings.storage.root) / "projects" / "frontend" / ".archive.lock"
    frontend_lock.parent.mkdir(parents=True, exist_ok=True)
    frontend_lock.write_text("", encoding="utf-8")
    (frontend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "repair", "Backend", "--yes"])
    assert result.exit_code == 0

    async def verify() -> tuple[list[FileReservation], list[FileReservation]]:
        async with get_session() as session:
            backend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "backend"))
                )
            ).scalars().all()
            frontend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "frontend"))
                )
            ).scalars().all()
            return list(backend_rows), list(frontend_rows)

    backend_rows, frontend_rows = asyncio.run(verify())
    assert backend_rows[0].released_ts is not None
    assert frontend_rows[0].released_ts is None
    assert backend_lock.exists() is False
    assert frontend_lock.exists() is True


def test_doctor_restore_creates_pre_restore_backup(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    calls: dict[str, Any] = {}

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        calls["reason"] = kwargs.get("reason")
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["restore_backup_path"] = args[1]
        calls["restore_dry_run"] = kwargs.get("dry_run")
        return {"database_restored": True, "bundles_restored": [], "errors": []}

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 0
    assert calls["reason"] == "pre-restore"
    assert calls["restore_backup_path"] == backup_path
    assert calls["restore_dry_run"] is False
    assert "Pre-restore backup:" in result.stdout


def test_doctor_restore_aborts_when_pre_restore_backup_fails(tmp_path, monkeypatch):
    runner = CliRunner()
    current_archive = tmp_path / "current-archive"
    (current_archive / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_ROOT", str(current_archive))
    clear_settings_cache()

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(current_archive),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    async def failing_create_backup(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("archive bundle failed")

    def should_not_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - defensive
        raise AssertionError("restore should not proceed when pre-restore backup fails")

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", failing_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", should_not_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Restore failed" in result.stdout
    assert "archive bundle failed" in result.stdout


def test_doctor_restore_dry_run_skips_pre_restore_backup(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    create_calls = 0
    restore_calls: list[bool | None] = []

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        nonlocal create_calls
        create_calls += 1
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        restore_calls.append(kwargs.get("dry_run"))
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": [],
            "would_restore_database": False,
            "would_restore_bundles": [],
        }

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--dry-run"])
    assert result.exit_code == 0
    assert create_calls == 0
    assert restore_calls == [True]


def test_doctor_restore_dry_run_exits_nonzero_when_preview_reports_errors(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "dry-run-error",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://agent:mail@localhost:5432/mcp")
    clear_settings_cache()

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--dry-run"])

    assert result.exit_code == 1
    assert "Dry run found restore blockers" in result.stdout
    assert "does not use a SQLite database file" in result.stdout


def test_doctor_repair_aborts_when_backup_creation_fails(isolated_env, monkeypatch):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key="/backend")
            session.add(backend)
            await session.commit()
            await session.refresh(backend)
            assert backend.id is not None

            backend_agent = Agent(
                project_id=backend.id,
                name="BlueLake",
                program="codex",
                model="gpt-5",
                task_description="",
            )
            session.add(backend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            assert backend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    async def failing_backup(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("backup disk offline")

    asyncio.run(seed())
    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", failing_backup)

    settings = get_settings()
    backend_lock = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    backend_lock.parent.mkdir(parents=True, exist_ok=True)
    backend_lock.write_text("", encoding="utf-8")
    (backend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "repair", "Backend", "--yes"])
    assert result.exit_code == 1
    assert "Backup failed" in result.stdout

    async def verify() -> list[FileReservation]:
        async with get_session() as session:
            backend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "backend"))
                )
            ).scalars().all()
            return list(backend_rows)

    backend_rows = asyncio.run(verify())
    assert backend_rows[0].released_ts is None
    assert backend_lock.exists() is True


def test_doctor_repair_exits_nonzero_when_repair_reports_errors(isolated_env, monkeypatch, tmp_path):
    runner = CliRunner()

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        return tmp_path / "fake-doctor-backup"

    async def failing_heal_locks(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("archive lock cleanup exploded")

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.heal_archive_locks", failing_heal_locks)

    result = runner.invoke(app, ["doctor", "repair", "--yes"])

    assert result.exit_code == 1
    assert "Lock healing failed" in result.stdout
    assert "Errors: 1" in result.stdout


def test_doctor_restore_rejects_malformed_manifest(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text("{not-json", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_rejects_manifest_without_restore_payload(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "empty",
            "database_path": None,
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_rejects_manifest_artifact_outside_backup(tmp_path):
    runner = CliRunner()
    external_bundle = tmp_path / "external.bundle"
    external_bundle.write_text("bundle", encoding="utf-8")

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "bad-paths",
            "database_path": None,
            "project_bundles": [str(external_bundle)],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_exits_nonzero_when_restore_reports_errors(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    payload_dir = backup_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "db-copy.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "restore-error",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": ["simulated restore failure"],
        }

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Restore completed with errors" in result.stdout
    assert "simulated restore failure" in result.stdout


def test_doctor_restore_skips_pre_restore_backup_on_empty_current_state(tmp_path, monkeypatch):
    runner = CliRunner()
    current_archive = tmp_path / "current-archive"
    current_archive.mkdir()
    current_db = tmp_path / "current-state" / "mail.db"
    monkeypatch.setenv("STORAGE_ROOT", str(current_archive))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{current_db}")
    clear_settings_cache()

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(current_archive),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    def should_not_create_backup(*args: Any, **kwargs: Any) -> Path:  # pragma: no cover - defensive
        raise AssertionError("pre-restore backup should be skipped for empty current state")

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"database_restored": True, "bundles_restored": [], "errors": []}

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", should_not_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 0
    assert "Pre-restore backup skipped" in result.stdout
    assert "Database restored" in result.stdout


def test_doctor_restore_rejects_manifest_with_missing_artifact(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "missing-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout
    assert "references missing artifact" in result.stdout


def test_doctor_restore_rejects_manifest_directory_artifact(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "payload" / "db-copy.sqlite3").mkdir(parents=True, exist_ok=True)
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "dir-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout
    assert "artifact is not a file" in result.stdout
