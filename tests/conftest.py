import asyncio
import contextlib
import gc
from pathlib import Path

import psutil
import pytest

from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import reset_database_state
from mcp_agent_mail.storage import clear_repo_cache

# CPU overload threshold - skip benchmark tests if ALL cores are at this level
CPU_OVERLOAD_THRESHOLD = 95.0


def is_cpu_overloaded() -> bool:
    """Check if all CPU cores are at 95%+ utilization.

    Returns True only when the system is under extreme load (all cores saturated),
    which would make timing-based benchmark tests unreliable.
    """
    # Sample CPU usage over 200ms per-core
    per_cpu = psutil.cpu_percent(interval=0.2, percpu=True)
    if not per_cpu:
        return False

    overloaded = sum(1 for usage in per_cpu if usage >= CPU_OVERLOAD_THRESHOLD)
    return overloaded == len(per_cpu)


def skip_if_cpu_overloaded() -> None:
    """Skip the current test if all CPU cores are at 95%+ utilization.

    Use this at the start of any test that asserts on wall-clock time.
    Prevents flaky benchmark tests when the system is under extreme load.
    """
    if is_cpu_overloaded():
        cores = psutil.cpu_count()
        pytest.skip(
            f"Skipping benchmark: system under extreme CPU load "
            f"(all {cores} cores at {CPU_OVERLOAD_THRESHOLD}%+ utilization)"
        )


@pytest.fixture(scope="function")
def event_loop():
    """Create a new event loop for each test function.

    This fixture ensures proper event loop cleanup on all platforms,
    particularly macOS where the default event loop policy can cause
    'Event loop is closed' errors if not handled properly.

    The fixture:
    1. Creates a fresh event loop for each test
    2. Properly shuts down async generators
    3. Cancels any pending tasks
    4. Closes the loop cleanly

    Note: In Python 3.14+, event loop policy management is deprecated.
    asyncio.new_event_loop() creates the appropriate loop type automatically.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    yield loop

    # Proper cleanup sequence
    try:
        # Cancel all pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()

        # Allow cancelled tasks to complete
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        # Shutdown async generators (Python 3.6+)
        loop.run_until_complete(loop.shutdown_asyncgens())

        # Shutdown default executor (Python 3.9+)
        if hasattr(loop, "shutdown_default_executor"):
            loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        pass  # Ignore cleanup errors
    finally:
        asyncio.set_event_loop(None)
        loop.close()


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


@pytest.fixture(autouse=True)
def _global_resource_cleanup():
    """Best-effort global cleanup to avoid FD leaks under low ulimit.

    Some tests don't opt into `isolated_env` but still touch the global engine/repo cache.
    With RLIMIT_NOFILE=256 (common on macOS), a small amount of leakage can cascade into
    EMFILE failures later in the suite.
    """
    yield

    # Close cached repo handles first.
    with contextlib.suppress(Exception):
        clear_repo_cache()

    # Dispose engine/pool state across tests.
    with contextlib.suppress(Exception):
        reset_database_state()

    with contextlib.suppress(Exception):
        clear_settings_cache()

    # Extra safety: close any Repo objects that escaped caching.
    with contextlib.suppress(Exception):
        from git import Repo

        gc.collect()
        for obj in gc.get_objects():
            if isinstance(obj, Repo):
                with contextlib.suppress(Exception):
                    obj.close()
