from __future__ import annotations

import asyncio
import base64
import concurrent.futures as _cf
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastmcp import Client
from git import Repo
from PIL import Image

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import get_sqlite_pre_restore_path, get_sqlite_sidecar_paths
from mcp_agent_mail.storage import (
    AsyncFileLock,
    cleanup_leaked_lockfile_fds,
    collect_lock_status,
    create_diagnostic_backup,
    ensure_archive,
    get_file_content,
    heal_archive_locks,
    list_backups,
    restore_from_backup,
)


@pytest.mark.asyncio
async def test_data_uri_embed_without_conversion(isolated_env, monkeypatch):
    # Disable server conversion so inline images remain as data URIs
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    from mcp_agent_mail import config as _config
    # Avoid asserting on a blind Exception type; just test settings cache clear path
    with contextlib.suppress(Exception):
        raise RuntimeError("noop")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Craft tiny red dot webp data URI
        payload = base64.b64encode(b"dummy").decode("ascii")
        body = f"Inline ![x](data:image/webp;base64,{payload})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "InlineImg",
                "body_md": body,
                "convert_images": False,
            },
        )
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments") or []
        assert any(att.get("type") == "inline" for att in attachments)


@pytest.mark.asyncio
async def test_missing_file_path_in_markdown_and_originals_toggle(isolated_env, monkeypatch):
    # Originals disabled then enabled
    storage = Path(get_settings().storage.root).expanduser().resolve()
    image_path = storage.parent / "nope.png"
    if image_path.exists():
        image_path.unlink()

    # First: originals disabled
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "false")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    registration_token = ""
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        reg = await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        registration_token = reg.data["registration_token"]
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
                "subject": "MissingPath",
                "body_md": f"![x]({image_path})",
            },
        )
        assert res.data.get("deliveries")

    # Now originals enabled
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server2 = build_mcp_server()
    async with Client(server2) as client2:
        await client2.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "GreenCastle",
                "registration_token": registration_token,
            },
        )
        res2 = await client2.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "sender_token": registration_token,
                "to": ["GreenCastle"],
                "subject": "MissingPath2",
                "body_md": f"![x]({image_path})",
            },
        )
        assert res2.data.get("deliveries")


@pytest.mark.asyncio
async def test_async_file_lock_recovers_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    lock_path = tmp_path / ".archive.lock"
    lock_path.touch()
    stale_time = time.time() - 120
    os.utime(lock_path, (stale_time, stale_time))
    metadata_path = tmp_path / f"{lock_path.name}.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": stale_time}))

    lock = AsyncFileLock(lock_path, timeout_seconds=0.1, stale_timeout_seconds=1.0)
    async with lock:
        current = json.loads(metadata_path.read_text())
        assert current.get("pid") == os.getpid()

    # Metadata should be cleaned up after release
    assert not metadata_path.exists()
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_create_and_list_diagnostic_backups(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Backup Seed",
                "body_md": "seed archive repo",
            },
        )
        assert res.data.get("deliveries")

    settings = get_settings()
    backup_path = await create_diagnostic_backup(settings, reason="test-backup")
    assert backup_path.exists()
    manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_bundles"] == ["archive.bundle"]

    backups = await list_backups(settings)
    assert any(item["path"] == str(backup_path) for item in backups)


@pytest.mark.asyncio
async def test_restore_from_backup_stages_bundle_inside_storage_root(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Restore Seed",
                "body_md": "seed archive repo",
            },
        )
        assert res.data.get("deliveries")

    settings = get_settings()
    backup_path = await create_diagnostic_backup(settings, reason="restore-check")
    result = await restore_from_backup(settings, backup_path)
    assert result["errors"] == []
    assert len(result["bundles_restored"]) == 1

    archive = await ensure_archive(settings, "backend")
    assert (archive.root / "messages").exists()


@pytest.mark.asyncio
async def test_create_diagnostic_backup_uses_unique_sanitized_directory_names(
    isolated_env,
    monkeypatch,
):
    settings = get_settings()
    await ensure_archive(settings, "backend")

    fixed_now = datetime(2026, 4, 10, 1, 2, 3, 456789, tzinfo=timezone.utc)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("mcp_agent_mail.storage.datetime", FrozenDateTime)

    first = await create_diagnostic_backup(settings, reason="pre/restore")
    second = await create_diagnostic_backup(settings, reason="pre/restore")

    expected_prefix = f"{fixed_now.strftime('%Y-%m-%dT%H-%M-%S-%f')}_pre-restore"
    assert first.name == expected_prefix
    assert second.name == f"{expected_prefix}_1"
    assert first.exists()
    assert second.exists()


@pytest.mark.asyncio
async def test_create_diagnostic_backup_normalizes_sqlite_sidecar_backup_names(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()
    wal_path, shm_path = get_sqlite_sidecar_paths(db_path)
    wal_path.write_text("wal-data", encoding="utf-8")
    shm_path.write_text("shm-data", encoding="utf-8")

    settings = get_settings()
    backup_path = await create_diagnostic_backup(settings, reason="custom-db")

    backup_wal, backup_shm = get_sqlite_sidecar_paths(backup_path / "database.sqlite3")
    assert backup_wal.read_text(encoding="utf-8") == "wal-data"
    assert backup_shm.read_text(encoding="utf-8") == "shm-data"
    assert (backup_path / wal_path.name).exists() is False
    assert (backup_path / shm_path.name).exists() is False
    manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_path"] == "database.sqlite3"
    assert manifest["project_bundles"] == []


@pytest.mark.asyncio
async def test_create_diagnostic_backup_rejects_empty_backup_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    settings = get_settings()
    with pytest.raises(ValueError, match="No database or archive bundle available to back up"):
        await create_diagnostic_backup(settings, reason="empty-install")

    backups = await list_backups(settings)
    assert backups == []


@pytest.mark.asyncio
async def test_create_diagnostic_backup_fails_closed_when_archive_bundle_creation_fails(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "mail.db"
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(archive_root))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()
    (archive_root / ".git").mkdir(parents=True, exist_ok=True)

    class FakeGit:
        def bundle(self, *args, **kwargs):
            raise RuntimeError("bundle failed")

    class FakeRepo:
        def __init__(self, path):
            self.path = path
            self.git = FakeGit()

        def close(self) -> None:
            pass

    monkeypatch.setattr("mcp_agent_mail.storage.Repo", FakeRepo)

    settings = get_settings()
    with pytest.raises(RuntimeError, match="Failed to create archive bundle backup"):
        await create_diagnostic_backup(settings, reason="bundle-failure")

    backups = await list_backups(settings)
    assert backups == []
    backup_dir = Path(settings.storage.root) / "backups"
    if backup_dir.exists():
        assert list(backup_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_list_backups_skips_manifests_with_missing_or_escaped_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()

    settings = get_settings()
    valid_backup = await create_diagnostic_backup(settings, reason="valid")

    backup_dir = Path(settings.storage.root) / "backups"
    missing_artifact_backup = backup_dir / "missing-artifact"
    missing_artifact_backup.mkdir(parents=True, exist_ok=True)
    (missing_artifact_backup / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "missing-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(Path(settings.storage.root)),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    escaped_artifact_backup = backup_dir / "escaped-artifact"
    escaped_artifact_backup.mkdir(parents=True, exist_ok=True)
    external_bundle = tmp_path / "outside.bundle"
    external_bundle.write_text("bundle", encoding="utf-8")
    (escaped_artifact_backup / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "escaped-bundle",
            "database_path": None,
            "project_bundles": [str(external_bundle)],
            "storage_root": str(Path(settings.storage.root)),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    directory_artifact_backup = backup_dir / "directory-artifact"
    directory_artifact_backup.mkdir(parents=True, exist_ok=True)
    (directory_artifact_backup / "payload").mkdir(parents=True, exist_ok=True)
    directory_payload = directory_artifact_backup / "payload" / "db-copy.sqlite3"
    directory_payload.mkdir()
    (directory_artifact_backup / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "directory-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(Path(settings.storage.root)),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    backups = await list_backups(settings)
    assert [item["path"] for item in backups] == [str(valid_backup)]


@pytest.mark.asyncio
async def test_restore_from_backup_replaces_and_cleans_sqlite_sidecars(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()
    wal_target, shm_target = get_sqlite_sidecar_paths(db_path)
    wal_target.write_text("stale-wal", encoding="utf-8")
    shm_target.write_text("stale-shm", encoding="utf-8")

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    database_copy = backup_path / "payload" / "db-copy.sqlite3"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )
    database_copy.write_text("fresh-db", encoding="utf-8")
    backup_wal, _backup_shm = get_sqlite_sidecar_paths(database_copy)
    backup_wal.write_text("fresh-wal", encoding="utf-8")

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)

    assert result["errors"] == []
    assert wal_target.read_text(encoding="utf-8") == "fresh-wal"
    assert shm_target.exists() is False

    pre_restore_db = get_sqlite_pre_restore_path(db_path)
    pre_restore_wal, pre_restore_shm = get_sqlite_sidecar_paths(pre_restore_db)
    assert pre_restore_db.exists()
    assert pre_restore_wal.read_text(encoding="utf-8") == "stale-wal"
    assert pre_restore_shm.read_text(encoding="utf-8") == "stale-shm"


@pytest.mark.asyncio
async def test_restore_from_backup_cleans_stale_pre_restore_snapshot_sidecars(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    db_path.write_text("live-db", encoding="utf-8")
    pre_restore_db = get_sqlite_pre_restore_path(db_path)
    pre_restore_wal, pre_restore_shm = get_sqlite_sidecar_paths(pre_restore_db)
    pre_restore_db.write_text("old-snapshot-db", encoding="utf-8")
    pre_restore_wal.write_text("old-snapshot-wal", encoding="utf-8")
    pre_restore_shm.write_text("old-snapshot-shm", encoding="utf-8")

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    database_copy = backup_path / "payload" / "db-copy.sqlite3"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    database_copy.write_text("fresh-db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "clean-snapshot",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)

    assert result["errors"] == []
    assert pre_restore_db.read_text(encoding="utf-8") == "live-db"
    assert pre_restore_wal.exists() is False
    assert pre_restore_shm.exists() is False


@pytest.mark.asyncio
async def test_restore_from_backup_cleans_stale_pre_restore_snapshot_when_current_db_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    pre_restore_db = get_sqlite_pre_restore_path(db_path)
    pre_restore_wal, pre_restore_shm = get_sqlite_sidecar_paths(pre_restore_db)
    pre_restore_db.parent.mkdir(parents=True, exist_ok=True)
    pre_restore_db.write_text("old-snapshot-db", encoding="utf-8")
    pre_restore_wal.write_text("old-snapshot-wal", encoding="utf-8")
    pre_restore_shm.write_text("old-snapshot-shm", encoding="utf-8")

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    database_copy = backup_path / "payload" / "db-copy.sqlite3"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    database_copy.write_text("fresh-db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "clean-missing-state",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)

    assert result["errors"] == []
    assert pre_restore_db.exists() is False
    assert pre_restore_wal.exists() is False
    assert pre_restore_shm.exists() is False


@pytest.mark.asyncio
async def test_restore_from_backup_creates_missing_database_parent_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "state" / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    database_copy = backup_path / "payload" / "db-copy.sqlite3"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "fresh-target",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )
    database_copy.write_text("fresh-db", encoding="utf-8")

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)

    assert result["errors"] == []
    assert result["database_restored"] is True
    assert db_path.read_text(encoding="utf-8") == "fresh-db"


@pytest.mark.asyncio
async def test_restore_from_backup_reports_missing_manifest_database_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "missing-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)
    assert result["database_restored"] is False
    assert result["errors"] == ["Database backup not found: payload/db-copy.sqlite3"]


@pytest.mark.asyncio
async def test_restore_from_backup_reports_non_file_database_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    sqlite3.connect(db_path).close()

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    directory_payload = backup_path / "payload" / "db-copy.sqlite3"
    directory_payload.mkdir(parents=True, exist_ok=True)
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "dir-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    result = await restore_from_backup(settings, backup_path)
    assert result["database_restored"] is False
    assert result["errors"] == ["Database backup artifact is not a file: payload/db-copy.sqlite3"]


@pytest.mark.asyncio
async def test_restore_from_backup_rejects_manifest_artifacts_outside_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    external_bundle = tmp_path / "external.bundle"
    external_bundle.write_text("bundle", encoding="utf-8")

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "bad-paths",
            "database_path": None,
            "project_bundles": [str(external_bundle)],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    with pytest.raises(ValueError, match="escapes backup directory"):
        await restore_from_backup(settings, backup_path)


@pytest.mark.asyncio
async def test_restore_from_backup_rejects_manifest_without_restore_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "empty",
            "database_path": None,
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()
    with pytest.raises(ValueError, match="database backup or at least one archive bundle"):
        await restore_from_backup(settings, backup_path)


@pytest.mark.asyncio
async def test_restore_from_backup_reports_missing_sqlite_target_for_database_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://agent:mail@localhost:5432/mcp")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    database_copy = backup_path / "payload" / "db-copy.sqlite3"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    database_copy.write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "postgres-target",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    settings = get_settings()

    dry_run_result = await restore_from_backup(settings, backup_path, dry_run=True)
    assert dry_run_result["would_restore_database"] is False
    assert dry_run_result["errors"] == [
        "Current configuration does not use a SQLite database file; cannot restore database payload"
    ]

    restore_result = await restore_from_backup(settings, backup_path)
    assert restore_result["database_restored"] is False
    assert restore_result["errors"] == [
        "Current configuration does not use a SQLite database file; cannot restore database payload"
    ]


@pytest.mark.asyncio
async def test_get_commit_queue_restarts_after_stop(monkeypatch):
    import mcp_agent_mail.storage as storage_module

    queue = storage_module._CommitQueue(max_wait_ms=1.0)
    await queue.start()
    await queue.stop()

    monkeypatch.setattr(storage_module, "_COMMIT_QUEUE", queue)
    monkeypatch.setattr(storage_module, "_COMMIT_QUEUE_LOCK", None)

    restarted = await storage_module._get_commit_queue()
    assert restarted is not queue
    assert restarted.stats["running"] is True

    await restarted.stop()
    monkeypatch.setattr(storage_module, "_COMMIT_QUEUE", None)
    monkeypatch.setattr(storage_module, "_COMMIT_QUEUE_LOCK", None)


@pytest.mark.asyncio
async def test_commit_queue_start_restarts_done_task():
    import mcp_agent_mail.storage as storage_module

    queue = storage_module._CommitQueue(max_wait_ms=1.0)
    stale_task = asyncio.create_task(asyncio.sleep(0))
    queue._task = stale_task
    await stale_task

    await queue.start()

    assert queue.stats["running"] is True
    assert queue._task is not stale_task

    await queue.stop()


@pytest.mark.asyncio
async def test_commit_queue_stop_drains_pending_requests(isolated_env, monkeypatch):
    import mcp_agent_mail.storage as storage_module

    queue = storage_module._CommitQueue(max_batch_size=1, max_wait_ms=1.0)
    settings = get_settings()
    repo_root = Path("/tmp/commit-queue-stop")
    first_started = asyncio.Event()
    second_enqueued = asyncio.Event()
    release_first = asyncio.Event()
    committed_messages: list[str] = []

    async def fake_commit_direct(
        repo_root_arg: Path,
        settings_arg,
        message: str,
        rel_paths,
    ) -> None:
        committed_messages.append(message)
        if len(committed_messages) == 1:
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr(storage_module, "_commit_direct", fake_commit_direct)
    original_put_nowait = queue._queue.put_nowait

    def put_nowait_and_signal(request) -> None:
        original_put_nowait(request)
        if request.message == "second":
            second_enqueued.set()

    monkeypatch.setattr(queue._queue, "put_nowait", put_nowait_and_signal)

    await queue.start()
    first_task = asyncio.create_task(queue.enqueue(repo_root, settings, "first", ["a.txt"]))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    second_task = asyncio.create_task(queue.enqueue(repo_root, settings, "second", ["b.txt"]))
    await asyncio.wait_for(second_enqueued.wait(), timeout=1.0)

    stop_task = asyncio.create_task(queue.stop(timeout_seconds=1.0))
    release_first.set()

    await asyncio.wait_for(stop_task, timeout=1.0)
    await asyncio.wait_for(first_task, timeout=1.0)
    await asyncio.wait_for(second_task, timeout=1.0)

    assert committed_messages == ["first", "second"]
    assert queue.stats["running"] is False


@pytest.mark.asyncio
async def test_commit_queue_batch_ignores_cancelled_waiter(isolated_env, monkeypatch):
    import mcp_agent_mail.storage as storage_module

    queue = storage_module._CommitQueue(max_wait_ms=1.0)
    settings = get_settings()
    committed_messages: list[str] = []

    async def fake_commit_direct(
        repo_root_arg: Path,
        settings_arg,
        message: str,
        rel_paths,
    ) -> None:
        committed_messages.append(message)

    monkeypatch.setattr(storage_module, "_commit_direct", fake_commit_direct)

    first_request = storage_module._CommitRequest(
        repo_root=Path("/tmp/commit-queue-cancel"),
        settings=settings,
        message="first",
        rel_paths=["a.txt"],
    )
    second_request = storage_module._CommitRequest(
        repo_root=Path("/tmp/commit-queue-cancel"),
        settings=settings,
        message="second",
        rel_paths=["b.txt"],
    )
    first_request.future.cancel()

    await queue._process_batch([first_request, second_request])

    assert first_request.future.cancelled() is True
    assert second_request.future.done() is True
    assert second_request.future.cancelled() is False
    assert second_request.future.exception() is None
    assert committed_messages == ["batch: 2 commits\n\n- first\n- second"]
    assert queue.stats["commits"] == 1


# ============================================================================
# cleanup_leaked_lockfile_fds — cross-platform deleted-lock-fd reaper (PR #164 / #116)
# ============================================================================


def test_cleanup_leaked_lockfile_fds_closes_deleted_lock_fd() -> None:
    """A still-open fd pointing at an unlinked ``.lock`` file must be closed.

    Reproduces the exact leak shape ``AsyncFileLock`` can leave behind (open the
    lock file, unlink while the fd stays open). Before the cross-platform fix the
    cleanup was a no-op on macOS (only checked ``/proc/self/fd``); the fix
    dispatches ``/dev/fd`` vs ``/proc/self/fd`` and uses ``st_nlink == 0`` as the
    deleted signal. On Linux (this CI) the ``/proc/self/fd`` path is exercised.
    """
    fd, path = tempfile.mkstemp(suffix=".lock")
    try:
        Path(path).unlink()
        # Sanity-check the leak setup: fd is open, but the inode has no links.
        assert os.fstat(fd).st_nlink == 0

        closed = cleanup_leaked_lockfile_fds()

        fd_dir = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
        if fd_dir.exists():
            assert closed >= 1, f"expected >=1 leaked lock fd closed; got {closed}"
            # The fd must now be closed: any further syscall on it raises EBADF.
            with pytest.raises(OSError):
                os.fstat(fd)
            fd = -1  # do not double-close in the finally
        else:
            # Platform without an fd directory: function early-returns 0.
            assert closed == 0
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def test_cleanup_leaked_lockfile_fds_skips_live_lock_fds(tmp_path: Path) -> None:
    """Live, on-disk ``.lock`` fds (st_nlink >= 1) must NOT be touched."""
    lock_path = tmp_path / "active.lock"
    lock_path.touch()
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        assert os.fstat(fd).st_nlink == 1  # still on disk
        cleanup_leaked_lockfile_fds()
        # The live fd must still be usable (not closed) after cleanup.
        assert os.fstat(fd).st_nlink == 1
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


def test_cleanup_leaked_lockfile_fds_ignores_non_lock_deleted_fds() -> None:
    """A deleted-but-open fd that is NOT a ``.lock`` file must be left alone."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        Path(path).unlink()
        assert os.fstat(fd).st_nlink == 0
        cleanup_leaked_lockfile_fds()
        # Non-lock fd survives: still a valid (deleted) inode we can fstat.
        assert os.fstat(fd).st_nlink == 0
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def test_cleanup_leaked_lockfile_fds_ignores_lockfile_substring_paths() -> None:
    """A deleted fd whose path contains ``.lock`` as a substring but whose basename
    does NOT end with ``.lock`` (e.g. ``/tmp/.lockfile-fooXYZ``) must NOT be reaped.

    This guards the anchored-basename predicate introduced to replace the
    over-broad ``".lock" not in path`` substring check.  The old check would
    have matched ``.lockfile-fooXYZ`` (substring match), silently closing an fd
    that belongs to an unrelated library.  The new check requires the basename
    to end with ``.lock`` or contain ``.lock.`` — neither is true for
    ``.lockfile-fooXYZ``.
    """
    fd_dir = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("no fd directory on this platform")

    # Create a deleted-but-open file whose path contains ".lock" as a substring
    # but whose basename is ".lockfile-fooXYZ" — NOT ending with ".lock".
    fd, path = tempfile.mkstemp(prefix=".lockfile-foo", suffix="XYZ")
    try:
        assert ".lock" in path, "sanity: substring '.lock' must be present in path"
        base = Path(path).name
        assert not base.endswith(".lock"), f"sanity: basename {base!r} must NOT end with '.lock'"
        Path(path).unlink()
        assert os.fstat(fd).st_nlink == 0  # confirmed deleted (leaked fd shape)

        closed = cleanup_leaked_lockfile_fds()

        # The fd must still be open — cleanup must NOT have reaped it.
        assert os.fstat(fd).st_nlink == 0, (
            f"cleanup_leaked_lockfile_fds incorrectly reaped fd for {path!r} "
            f"(basename {base!r}) — anchored-basename filter is broken"
        )
        # closed count should not include our fd (it may be > 0 if other genuine
        # leaked lock fds existed in the process, but our fd must survive).
        _ = closed  # not asserting == 0 to avoid flakiness from concurrent tests
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


# ============================================================================
# Lock release on interrupted writes + doctor staleness reconciliation (#166)
# ============================================================================


def test_async_file_lock_releases_across_executor_threads(tmp_path: Path) -> None:
    """Regression for issue #166: acquire on one worker thread, release on another.

    ``AsyncFileLock`` drives ``SoftFileLock.acquire``/``release`` through
    ``asyncio.to_thread``, so a single lock lifetime can span different executor
    threads. With filelock's default ``thread_local=True`` the release on a
    different thread is a silent no-op — the fd stays open and the ``.lock`` file
    is never removed, wedging every subsequent writer. ``AsyncFileLock`` must
    construct the lock with ``thread_local=False`` so cross-thread release works.
    """
    lock_path = tmp_path / ".archive.lock"
    lock = AsyncFileLock(lock_path)

    ex_a = _cf.ThreadPoolExecutor(max_workers=1)
    ex_b = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        ex_a.submit(lambda: lock._lock.acquire(timeout=5)).result()
        assert lock_path.exists(), "lock file should exist while held"
        # Release on a DIFFERENT thread than the one that acquired.
        ex_b.submit(lambda: lock._lock.release()).result()
        assert not lock_path.exists(), (
            "lock file must be removed after cross-thread release; a lingering "
            "lock file means the thread-local context regression is back (#166)"
        )
    finally:
        ex_a.shutdown(wait=True)
        ex_b.shutdown(wait=True)


@pytest.mark.asyncio
async def test_get_file_content_reuses_git_object_stream(isolated_env) -> None:
    """Reading one blob must leave GitPython's shared cat-file stream usable."""
    archive = await ensure_archive(get_settings(), "repeated-blob-reads")
    contents = {"first.txt": "first blob\n", "second.txt": "second blob\n", "empty.txt": ""}
    paths = []
    for name, content in contents.items():
        path = archive.root / name
        path.write_text(content, encoding="utf-8")
        paths.append(str(path.relative_to(archive.repo_root)))
    archive.repo.index.add(paths)
    commit = archive.repo.index.commit("Add blobs for repeated content reads")

    for commit_sha in (None, commit.hexsha):
        for name, content in contents.items():
            assert await get_file_content(archive, name, commit_sha=commit_sha) == content
    assert await get_file_content(archive, "first.txt") == contents["first.txt"]


@pytest.mark.asyncio
async def test_archive_write_lock_releases_on_body_exception(tmp_path: Path, monkeypatch) -> None:
    """The archive write lock is released even when the guarded body raises (#166)."""
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    from mcp_agent_mail.storage import ProjectArchive, archive_write_lock

    lock_path = tmp_path / ".archive.lock"
    with Repo.init(tmp_path) as repo:
        archive = ProjectArchive(
            settings=get_settings(),
            slug="t",
            root=tmp_path,
            repo=repo,
            lock_path=lock_path,
            repo_root=tmp_path,
        )

        with contextlib.suppress(RuntimeError):
            async with archive_write_lock(archive, timeout_seconds=5.0):
                assert lock_path.exists()
                raise RuntimeError("simulated interrupted write")

        # finally must have released the lock regardless of the exception.
        assert not lock_path.exists(), "archive lock must be released after a failed write (#166)"
        # And the lock must be immediately re-acquirable (not wedged).
        async with archive_write_lock(archive, timeout_seconds=5.0):
            assert lock_path.exists()
        assert not lock_path.exists()


@pytest.mark.asyncio
async def test_doctor_check_and_repair_agree_on_aged_live_lock(tmp_path: Path, monkeypatch) -> None:
    """doctor check (collect_lock_status) and doctor repair (heal_archive_locks) must agree (#166).

    An aged lock whose owner process is still alive (the wedged-server case) was
    flagged stale by ``collect_lock_status`` but skipped by ``heal_archive_locks``
    ("No stale locks to heal") because repair forced ``stale_timeout=0`` whenever
    the ``.owner.json`` sidecar was present. After reconciliation both apply the
    same age threshold.
    """
    settings = get_settings()
    root = Path(settings.storage.root).expanduser().resolve()
    proj_dir = root / "projects" / "wedged-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)

    lock_path = proj_dir / ".archive.lock"
    lock_path.touch()
    # Aged well beyond the 180s default stale threshold...
    aged = time.time() - 600
    os.utime(lock_path, (aged, aged))
    metadata_path = proj_dir / f"{lock_path.name}.owner.json"
    # ...but owned by THIS still-alive process (the wedged-server shape).
    metadata_path.write_text(json.dumps({"pid": os.getpid(), "created_ts": aged}))

    # doctor check: must flag it as stale (age-based).
    status = collect_lock_status(settings, project_slug="wedged-proj")
    flagged = [
        lock_info for lock_info in status["locks"]
        if lock_info.get("stale_suspected") and lock_info.get("path") == str(lock_path)
    ]
    assert flagged, "doctor check should flag an aged lock as stale even with a live owner"

    # doctor repair: must actually heal the very same lock.
    result = await heal_archive_locks(settings, project_slug="wedged-proj")
    assert str(lock_path) in result["locks_removed"], (
        "doctor repair must heal the aged lock that doctor check flagged (#166)"
    )
    assert not lock_path.exists()


# ============================================================================
# SHA256 content-addressable storage migration tests (F5)
# ============================================================================


@pytest.mark.asyncio
async def test_store_image_writes_sha256_path(isolated_env):
    """New writes use SHA256 (64-char hex) filenames, not SHA1 (40-char).

    Regression guard for the F5 upgrade: _store_image must derive the
    content-addressable storage key from hashlib.sha256, producing a 64-char
    hex digest.  The resulting .webp file must live under
    ``attachments/<digest[:2]>/<digest>.webp`` inside the project archive.
    """
    from mcp_agent_mail.storage import _store_image

    settings = get_settings()
    archive = await ensure_archive(settings, "test-sha256-write")

    # Build a minimal valid PNG in memory (1x1 red pixel)
    img_buf = __import__("io").BytesIO()
    pil = Image.new("RGB", (1, 1), color=(255, 0, 0))
    pil.save(img_buf, format="PNG")
    raw_bytes = img_buf.getvalue()

    # Write the image to a temp file so _store_image can read it
    img_file = archive.repo_root / "test_sha256_input.png"
    img_file.write_bytes(raw_bytes)

    try:
        meta, rel_path = await _store_image(archive, img_file, embed_policy="file")

        # The digest in the returned meta must be 64 hex chars (SHA256)
        digest = meta["sha1"]  # field name kept for compat; value is now SHA256
        assert isinstance(digest, str), "digest must be a string"
        assert len(digest) == 64, (
            f"SHA256 hex digest must be 64 chars, got {len(digest)}: {digest!r}"
        )
        # Verify it is a valid hex string
        int(digest, 16)

        # The actual file must exist at the expected SHA256-derived path
        expected_path = archive.attachments_dir / digest[:2] / f"{digest}.webp"
        assert expected_path.exists(), (
            f"Expected webp at SHA256 path {expected_path} — file not found"
        )

        # rel_path must reference the SHA256 filename
        assert rel_path is not None
        assert digest in rel_path, (
            f"rel_path {rel_path!r} must contain the SHA256 digest"
        )

        # The manifest file must exist and contain the sha1 field (compat key)
        manifest_path = archive.root / "attachments" / "_manifests" / f"{digest}.json"
        assert manifest_path.exists(), "per-attachment manifest must be written"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["sha1"] == digest, "manifest sha1 field must match digest"

        # Cross-check: SHA1 of the same bytes is 40 chars and DIFFERENT
        sha1_digest = hashlib.sha1(raw_bytes, usedforsecurity=False).hexdigest()
        assert len(sha1_digest) == 40
        assert sha1_digest != digest, "SHA256 and SHA1 of same data must differ"

        # The OLD SHA1-keyed path must NOT exist (we only write SHA256 now)
        legacy_path = archive.attachments_dir / sha1_digest[:2] / f"{sha1_digest}.webp"
        assert not legacy_path.exists(), (
            "No SHA1-keyed webp should be written for new content"
        )
    finally:
        img_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_legacy_sha1_blob_readable_alongside_sha256(isolated_env):
    """Legacy SHA1-named blobs remain on disk; SHA256-named new blobs coexist.

    This test simulates the migration scenario: a blob written by an older
    version of the code (stored at the 40-char SHA1 path) still exists on
    disk, and a new write produces a 64-char SHA256 path.  Both files must
    be independently readable — no collision, no overwrite.
    """
    from mcp_agent_mail.storage import _store_image

    settings = get_settings()
    archive = await ensure_archive(settings, "test-sha256-legacy-coexist")

    # Build two distinct minimal images
    def _make_png(color: tuple[int, int, int]) -> bytes:
        buf = __import__("io").BytesIO()
        Image.new("RGB", (1, 1), color=color).save(buf, format="PNG")
        return buf.getvalue()

    red_bytes = _make_png((255, 0, 0))
    blue_bytes = _make_png((0, 0, 255))

    # Manually plant a "legacy" SHA1-keyed blob (simulating old code output)
    sha1_of_red = hashlib.sha1(red_bytes, usedforsecurity=False).hexdigest()
    legacy_dir = archive.attachments_dir / sha1_of_red[:2]
    legacy_dir.mkdir(parents=True, exist_ok=True)
    # Write a fake .webp at the SHA1 path (content doesn't need to be real webp)
    legacy_webp = legacy_dir / f"{sha1_of_red}.webp"
    legacy_webp.write_bytes(b"LEGACY_BLOB")

    # Now write a fresh image via _store_image (should produce SHA256 path)
    blue_file = archive.repo_root / "test_blue_input.png"
    blue_file.write_bytes(blue_bytes)
    try:
        meta, _rel_path = await _store_image(archive, blue_file, embed_policy="file")
        new_digest = meta["sha1"]

        assert isinstance(new_digest, str), "digest must be a string"
        assert len(new_digest) == 64, "fresh write must use 64-char SHA256 digest"

        # Both paths must coexist and be readable
        assert legacy_webp.exists(), "legacy SHA1 blob must not be disturbed"
        assert legacy_webp.read_bytes() == b"LEGACY_BLOB", "legacy blob content must be intact"

        new_webp = archive.attachments_dir / new_digest[:2] / f"{new_digest}.webp"
        assert new_webp.exists(), "SHA256-keyed new blob must exist"
        assert new_webp.stat().st_size > 0, "new blob must be non-empty"

        # Sanity: SHA1 of blue_bytes vs SHA1 of red_bytes differ
        sha1_of_blue = hashlib.sha1(blue_bytes, usedforsecurity=False).hexdigest()
        assert sha1_of_blue != sha1_of_red, "test images must have distinct SHA1 digests"

        # No cross-contamination: the new write must not touch the legacy file
        assert legacy_webp.read_bytes() == b"LEGACY_BLOB"
    finally:
        blue_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_store_image_sha256_via_mcp_send_message(isolated_env, monkeypatch):
    """End-to-end: send_message with an image attachment produces a SHA256 path.

    Verifies the full MCP → _store_image pipeline: the attachment metadata
    returned in the delivery payload must carry a 64-char digest under the
    ``sha1`` key (compat field name).  We enable ALLOW_ABSOLUTE_ATTACHMENT_PATHS
    so that the absolute image path in the markdown body is resolved and stored.
    """
    monkeypatch.setenv("ALLOW_ABSOLUTE_ATTACHMENT_PATHS", "true")
    from mcp_agent_mail import config as _conf
    _conf.clear_settings_cache()

    settings = get_settings()
    storage_root = Path(settings.storage.root).expanduser().resolve()

    # Build a 32x32 PNG — large enough to exceed the 128-byte inline threshold
    # after WebP conversion, ensuring a "file" type attachment with a path.
    img_buf = __import__("io").BytesIO()
    Image.new("RGB", (32, 32), color=(0, 128, 255)).save(img_buf, format="PNG")
    img_path = storage_root.parent / "test_sha256_e2e.png"
    img_path.write_bytes(img_buf.getvalue())

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        reg = await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        agent_name = reg.data.get("name", "BlueLake")
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "SHA256 path test",
                "body_md": f"![img]({img_path})",
            },
        )
        deliveries = res.data.get("deliveries") or []
        assert deliveries, "at least one delivery expected"
        attachments = deliveries[0].get("payload", {}).get("attachments") or []
        assert attachments, "attachment must be present in delivery"

        att = attachments[0]
        digest = att.get("sha1")
        assert digest is not None, "attachment metadata must include 'sha1' field"
        assert len(digest) == 64, (
            f"digest in attachment metadata must be 64-char SHA256, got {len(digest)}: {digest!r}"
        )

        # For file-type attachments, also verify the .webp exists on disk.
        if att.get("type") == "file":
            att_path = att.get("path")
            assert att_path, "file attachment must have a 'path' field"
            full_path = storage_root / att_path
            assert full_path.exists(), f"webp at {att_path} not found on disk"

    img_path.unlink(missing_ok=True)
