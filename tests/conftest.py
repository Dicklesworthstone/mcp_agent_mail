from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import reset_database_state


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Provide isolated database settings for tests and reset caches."""
    db_path: Path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    get_settings.cache_clear()
    reset_database_state()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_database_state()
        if db_path.exists():
            db_path.unlink()
