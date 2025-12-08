"""Async database engine and session management utilities."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .config import DatabaseSettings, Settings, get_settings

T = TypeVar("T")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_schema_ready = False
_schema_lock: asyncio.Lock | None = None


def retry_on_db_lock(max_retries: int = 5, base_delay: float = 0.1, max_delay: float = 5.0) -> Callable[..., Any]:
    """Decorator to retry async functions on SQLite database lock errors with exponential backoff + jitter.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds (will be exponentially increased)
        max_delay: Maximum delay between retries in seconds

    This handles transient "database is locked" errors from SQLite by:
    1. Catching OperationalError with lock-related messages
    2. Waiting with exponential backoff: base_delay * (2 ** attempt)
    3. Adding jitter to prevent thundering herd: random ±25% of delay
    4. Giving up after max_retries and re-raising the error
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except OperationalError as e:
                    # Check if it's a lock-related error
                    error_msg = str(e).lower()
                    is_lock_error = any(
                        phrase in error_msg
                        for phrase in ["database is locked", "database is busy", "locked"]
                    )

                    if not is_lock_error or attempt >= max_retries:
                        # Not a lock error, or we've exhausted retries - raise it
                        raise

                    last_exception = e

                    # Calculate exponential backoff with jitter
                    delay = min(base_delay * (2**attempt), max_delay)
                    jitter = delay * 0.25 * (2 * random.random() - 1)  # ±25% jitter
                    total_delay = delay + jitter

                    # Log the retry (if logging is available)
                    import logging

                    func_name = getattr(func, "__name__", getattr(func, "__qualname__", "<callable>"))
                    logging.warning(
                        f"Database locked, retrying {func_name} "
                        f"(attempt {attempt + 1}/{max_retries}) after {total_delay:.2f}s"
                    )

                    await asyncio.sleep(total_delay)

            # Should never reach here, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry loop exit")

        return wrapper

    return decorator


def _build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build async SQLAlchemy engine with SQLite-optimized settings for concurrency."""
    from sqlalchemy import event

    # For SQLite, enable WAL mode and set timeout for better concurrent access
    connect_args = {}
    is_sqlite = "sqlite" in settings.url.lower()

    if is_sqlite:
        # Register datetime adapters ONCE globally for Python 3.12+ compatibility
        # These are module-level registrations, not per-connection
        import datetime as dt_module
        import sqlite3

        def adapt_datetime_iso(val: Any) -> str:
            """Adapt datetime.datetime to ISO 8601 date."""
            return str(val.isoformat())

        def convert_datetime(val: bytes | str) -> dt_module.datetime | None:
            """Convert ISO 8601 datetime to datetime.datetime object.

            Returns None for any conversion errors (invalid format, wrong type,
            corrupted data, etc.) to allow graceful degradation rather than crashing.
            """
            try:
                # Handle both bytes and str (SQLite can return either)
                if isinstance(val, bytes):
                    val = val.decode('utf-8')
                return dt_module.datetime.fromisoformat(val)
            except (ValueError, AttributeError, TypeError, UnicodeDecodeError, OverflowError):
                # Return None for any conversion failure:
                # - ValueError: invalid ISO format string
                # - TypeError: unexpected type (shouldn't happen but defensive)
                # - AttributeError: val has no expected attributes (defensive)
                # - UnicodeDecodeError: corrupted bytes (extreme edge case)
                # - OverflowError: datetime value out of valid range (year outside 1-9999)
                return None

        # Register adapters globally (safe to call multiple times - last registration wins)
        sqlite3.register_adapter(dt_module.datetime, adapt_datetime_iso)
        sqlite3.register_converter("timestamp", convert_datetime)

        connect_args = {
            "timeout": 30.0,  # Wait up to 30 seconds for lock (default is 5)
            "check_same_thread": False,  # Required for async SQLite
        }

    engine = create_async_engine(
        settings.url,
        echo=settings.echo,
        future=True,
        pool_pre_ping=True,
        pool_size=25,  # Increased for high-concurrency workloads
        max_overflow=25,  # Allow up to 50 total connections under load
        pool_timeout=30,  # Fail fast with clear error instead of hanging indefinitely
        pool_recycle=3600,  # Recycle connections after 1 hour to prevent stale handles
        connect_args=connect_args,
    )

    # For SQLite: Set up event listener to configure each connection with WAL mode
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, connection_record: Any) -> None:
            """Set SQLite PRAGMAs for better concurrent performance on each connection."""
            cursor = dbapi_conn.cursor()
            # Enable WAL mode for concurrent reads/writes
            cursor.execute("PRAGMA journal_mode=WAL")
            # Use NORMAL synchronous mode (safer than OFF, faster than FULL)
            cursor.execute("PRAGMA synchronous=NORMAL")
            # Set busy timeout (wait up to 30 seconds for locks)
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def init_engine(settings: Settings | None = None) -> None:
    """Initialise global engine and session factory once."""
    global _engine, _session_factory
    if _engine is not None and _session_factory is not None:
        return
    resolved_settings = settings or get_settings()
    engine = _build_engine(resolved_settings.database)
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


@retry_on_db_lock(max_retries=5, base_delay=0.1, max_delay=5.0)
async def ensure_schema(settings: Settings | None = None) -> None:
    """Ensure database schema exists (creates tables from SQLModel definitions).

    This is the pure SQLModel approach:
    - Models define the schema
    - create_all() creates tables that don't exist yet
    - For schema changes: delete the DB and regenerate (dev) or use Alembic (prod)

    Also enables SQLite WAL mode for better concurrent access.
    """
    global _schema_ready, _schema_lock
    if _schema_ready:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ready:
            return
        init_engine(settings)
        engine = get_engine()
        async with engine.begin() as conn:
            # Pure SQLModel: create tables from metadata
            # (WAL mode is set automatically via event listener in _build_engine)
            await conn.run_sync(SQLModel.metadata.create_all)
            # Setup FTS and custom indexes
            await conn.run_sync(_setup_fts)
        _schema_ready = True


def reset_database_state() -> None:
    """Test helper to reset global engine/session state."""
    global _engine, _session_factory, _schema_ready, _schema_lock
    _engine = None
    _session_factory = None
    _schema_ready = False
    _schema_lock = None


def _setup_fts(connection: Any) -> None:
    connection.exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(message_id UNINDEXED, subject, body)"
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_ai
        AFTER INSERT ON messages
        BEGIN
            INSERT INTO fts_messages(rowid, message_id, subject, body)
            VALUES (new.id, new.id, new.subject, new.body_md);
        END;
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_ad
        AFTER DELETE ON messages
        BEGIN
            DELETE FROM fts_messages WHERE rowid = old.id;
        END;
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_au
        AFTER UPDATE ON messages
        BEGIN
            DELETE FROM fts_messages WHERE rowid = old.id;
            INSERT INTO fts_messages(rowid, message_id, subject, body)
            VALUES (new.id, new.id, new.subject, new.body_md);
        END;
        """
    )
    # Additional performance indexes for common access patterns
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_created_ts ON messages(created_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_importance ON messages(importance)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_sender_created ON messages(sender_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_project_created ON messages(project_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_expires_ts ON file_reservations(expires_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_message_recipients_agent ON message_recipients(agent_id)"
    )

