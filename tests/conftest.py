import contextlib
import gc
from pathlib import Path

import pytest

from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import reset_database_state
from mcp_agent_mail.storage import clear_repo_cache


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Provide isolated database settings for tests and reset caches."""
    db_path: Path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test-agent")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "128")
    clear_settings_cache()
    reset_database_state()
    # Clear repo cache before test to ensure isolation
    clear_repo_cache()
    try:
        yield
    finally:
        # Close all cached Repo objects first (prevents file handle leaks)
        clear_repo_cache()

        # Suppress ResourceWarnings during cleanup since Python 3.14 warns about resources
        # being cleaned up by GC, which is exactly what we want
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ResourceWarning)
            try:
                import time

                from git import Repo

                # Multiple GC passes to ensure full cleanup of any non-cached repos
                for _ in range(2):
                    gc.collect()
                    # Close any Repo instances that might still be open
                    for obj in gc.get_objects():
                        if isinstance(obj, Repo):
                            with contextlib.suppress(Exception):
                                obj.close()

                # Give subprocesses time to terminate
                time.sleep(0.05)

                # Final GC pass
                gc.collect()
            except Exception:
                pass

            # Force another GC to clean up any remaining references
            gc.collect()

        clear_settings_cache()
        reset_database_state()

        if db_path.exists():
            db_path.unlink()
        storage_root = tmp_path / "storage"
        if storage_root.exists():
            for path in storage_root.rglob("*"):
                if path.is_file():
                    path.unlink()
            for path in sorted(storage_root.rglob("*"), reverse=True):
                if path.is_dir():
                    path.rmdir()
            if storage_root.exists():
                storage_root.rmdir()
