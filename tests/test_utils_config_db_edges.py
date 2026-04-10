from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import (
    ensure_schema,
    get_engine,
    get_sqlite_pre_restore_path,
    get_sqlite_sidecar_paths,
    reset_database_state,
)
from mcp_agent_mail.utils import sanitize_agent_name, slugify


def test_slugify_and_sanitize_edges():
    assert slugify("  Hello World!!  ") == "hello-world"
    assert slugify("") == "project"
    assert sanitize_agent_name(" A!@#$ ") == "A"
    assert sanitize_agent_name("!!!") is None


def test_config_csv_and_bool_parsing(monkeypatch):
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader, ro ,, read ")
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    clear_settings_cache()
    s = get_settings()
    assert {"reader", "ro", "read"}.issubset(set(s.http.rbac_reader_roles))
    assert s.http.rate_limit_enabled is True


def test_database_pool_size_default_is_50(monkeypatch):
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    clear_settings_cache()
    s = get_settings()
    assert s.database.pool_size == 50


def test_get_settings_cache_clear_reloads_decouple_snapshot(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("HTTP_PORT=1111\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HTTP_PORT", raising=False)
    clear_settings_cache()
    assert get_settings().http.port == 1111

    env_path.write_text("HTTP_PORT=2222\n", encoding="utf-8")
    get_settings.cache_clear()
    assert get_settings().http.port == 2222


def test_db_engine_reset_and_reinit(isolated_env):
    # Reset and ensure engine can be re-initialized and schema ensured
    reset_database_state()
    # Access engine should lazy-init
    _ = get_engine()
    # Ensure schema executes without error
    asyncio.run(ensure_schema())


def test_sqlite_sidecar_paths_preserve_database_filename():
    wal_path, shm_path = get_sqlite_sidecar_paths(Path("/tmp/mail.db"))
    assert wal_path == Path("/tmp/mail.db-wal")
    assert shm_path == Path("/tmp/mail.db-shm")
    assert get_sqlite_pre_restore_path(Path("/tmp/mail.db")) == Path("/tmp/mail.db.pre-restore")
