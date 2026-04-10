from __future__ import annotations

import base64
import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import get_sqlite_pre_restore_path, get_sqlite_sidecar_paths
from mcp_agent_mail.storage import (
    AsyncFileLock,
    create_diagnostic_backup,
    ensure_archive,
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
