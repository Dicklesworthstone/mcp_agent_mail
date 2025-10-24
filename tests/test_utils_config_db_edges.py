from __future__ import annotations

import asyncio

from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_engine, reset_database_state
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


def test_db_engine_reset_and_reinit(isolated_env):
    # Reset and ensure engine can be re-initialized and schema ensured
    reset_database_state()
    # Access engine should lazy-init
    _ = get_engine()
    # Ensure schema executes without error
    asyncio.run(ensure_schema())


