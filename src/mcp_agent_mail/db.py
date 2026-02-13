"""Async database engine and session management utilities.

This module provides robust SQLite handling for high-concurrency multi-agent workloads:

Concurrency Architecture:
- WAL mode with optimized checkpoint strategy (passive checkpoints to avoid blocking)
- Connection pooling with conservative limits to prevent file descriptor exhaustion
- Exponential backoff with jitter on lock contention (prevents thundering herd)
- Circuit breaker pattern to fail fast during prolonged database issues

Key invariants:
- One writer at a time (SQLite constraint), concurrent readers allowed
- Connections recycled after 1 hour to prevent stale handle accumulation
- Pool timeout of 30s fails fast with clear error vs hanging indefinitely
- busy_timeout of 60s gives writers time to complete during checkpoint
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .config import DatabaseSettings, Settings, clear_settings_cache, get_settings

T = TypeVar("T")
_logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_schema_ready = False
_schema_lock: asyncio.Lock | None = None

# Circuit breaker state for database operations
_circuit_breaker_failures: int = 0
_circuit_breaker_last_failure: float = 0.0
_circuit_breaker_open_until: float = 0.0
_CIRCUIT_BREAKER_THRESHOLD: int = 5  # Failures before opening circuit
_CIRCUIT_BREAKER_RESET_SECONDS: float = 30.0  # Time before half-open state
_CIRCUIT_BREAKER_LOCK: asyncio.Lock | None = None


class CircuitState(Enum):
    """Circuit breaker states for database operations."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing fast, not attempting operations
    HALF_OPEN = "half_open"  # Testing if service recovered


def _get_circuit_breaker_lock() -> asyncio.Lock:
    """Get or create circuit breaker lock (must be called from async context)."""
    global _CIRCUIT_BREAKER_LOCK
    if _CIRCUIT_BREAKER_LOCK is None:
        _CIRCUIT_BREAKER_LOCK = asyncio.Lock()
    return _CIRCUIT_BREAKER_LOCK


def get_circuit_state() -> CircuitState:
    """Get current circuit breaker state (non-blocking check)."""
    global _circuit_breaker_open_until, _circuit_breaker_failures
    now = time.monotonic()
    if _circuit_breaker_open_until > now:
        return CircuitState.OPEN
    if _circuit_breaker_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        # Circuit was open but timeout passed - now half-open
        return CircuitState.HALF_OPEN
    return CircuitState.CLOSED


async def _record_circuit_success() -> None:
    """Record successful operation - reset circuit breaker."""
    global _circuit_breaker_failures, _circuit_breaker_open_until
    async with _get_circuit_breaker_lock():
        _circuit_breaker_failures = 0
        _circuit_breaker_open_until = 0.0


async def _record_circuit_failure() -> None:
    """Record failed operation - potentially open circuit breaker."""
    global _circuit_breaker_failures, _circuit_breaker_last_failure, _circuit_breaker_open_until
    async with _get_circuit_breaker_lock():
        now = time.monotonic()
        _circuit_breaker_failures += 1
        _circuit_breaker_last_failure = now
        if _circuit_breaker_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            _circuit_breaker_open_until = now + _CIRCUIT_BREAKER_RESET_SECONDS
            _logger.warning(
                "circuit_breaker.opened",
                extra={
                    "failures": _circuit_breaker_failures,
                    "reset_seconds": _CIRCUIT_BREAKER_RESET_SECONDS,
                },
            )


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and operation should not proceed."""
    pass

_QUERY_TRACKER: contextvars.ContextVar["QueryTracker | None"] = contextvars.ContextVar("query_tracker", default=None)
_QUERY_HOOKS_INSTALLED = False
_SLOW_QUERY_LIMIT = 50
_SQL_TABLE_RE = re.compile(r"\bfrom\s+([\w\.\"`\[\]]+)", re.IGNORECASE)
_SQL_UPDATE_RE = re.compile(r"\bupdate\s+([\w\.\"`\[\]]+)", re.IGNORECASE)
_SQL_INSERT_RE = re.compile(r"\binsert\s+into\s+([\w\.\"`\[\]]+)", re.IGNORECASE)


@dataclass(slots=True)
class QueryTracker:
    total: int = 0
    total_time_ms: float = 0.0
    per_table: dict[str, int] = field(default_factory=dict)
    slow_query_ms: float | None = None
    slow_queries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, statement: str, duration_ms: float) -> None:
        self.total += 1
        self.total_time_ms += duration_ms
        table = _extract_table_name(statement)
        if table:
            self.per_table[table] = self.per_table.get(table, 0) + 1
        if (
            self.slow_query_ms is not None
            and duration_ms >= self.slow_query_ms
            and len(self.slow_queries) < _SLOW_QUERY_LIMIT
        ):
            self.slow_queries.append(
                {
                    "table": table,
                    "duration_ms": round(duration_ms, 2),
                }
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "total_time_ms": round(self.total_time_ms, 2),
            "per_table": dict(sorted(self.per_table.items(), key=lambda item: (-item[1], item[0]))),
            "slow_query_ms": self.slow_query_ms,
            "slow_queries": list(self.slow_queries),
        }


def _clean_table_name(raw: str) -> str:
    cleaned = raw.strip()
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned.strip("`\"[]")


def _extract_table_name(statement: str) -> str | None:
    for pattern in (_SQL_INSERT_RE, _SQL_UPDATE_RE, _SQL_TABLE_RE):
        match = pattern.search(statement)
        if match:
            return _clean_table_name(match.group(1))
    return None


def get_query_tracker() -> QueryTracker | None:
    return _QUERY_TRACKER.get()


def start_query_tracking(*, slow_ms: float | None = None) -> tuple[QueryTracker, contextvars.Token]:
    tracker = QueryTracker(slow_query_ms=slow_ms)
    token = _QUERY_TRACKER.set(tracker)
    return tracker, token


def stop_query_tracking(token: contextvars.Token) -> None:
    _QUERY_TRACKER.reset(token)


@contextmanager
def track_queries(*, slow_ms: float | None = None) -> Iterator[QueryTracker]:
    tracker, token = start_query_tracking(slow_ms=slow_ms)
    try:
        yield tracker
    finally:
        stop_query_tracking(token)


def _is_lock_error(error_msg: str) -> bool:
    """Check if error message indicates a database lock error."""
    lower_msg = error_msg.lower()
    return any(
        phrase in lower_msg
        for phrase in [
            "database is locked",
            "database is busy",
            "locked",
            "unable to open database",  # Can happen during checkpoint
            "disk i/o error",  # Sometimes masks lock issues
        ]
    )


def _is_pool_exhausted_error(exc: Exception) -> bool:
    """Check if exception indicates connection pool exhaustion."""
    if isinstance(exc, SATimeoutError):
        return True
    error_msg = str(exc).lower()
    return "pool" in error_msg and ("timeout" in error_msg or "exhausted" in error_msg)


def retry_on_db_lock(
    max_retries: int = 7,
    base_delay: float = 0.05,
    max_delay: float = 8.0,
    use_circuit_breaker: bool = True,
) -> Callable[..., Any]:
    """Decorator to retry async functions on SQLite database lock errors with exponential backoff + jitter.

    Args:
        max_retries: Maximum number of retry attempts (default: 7 for ~12.7s total backoff)
        base_delay: Initial delay in seconds (default: 0.05s for faster initial retry)
        max_delay: Maximum delay between retries in seconds
        use_circuit_breaker: Whether to integrate with circuit breaker (default: True)

    This handles transient "database is locked" errors from SQLite by:
    1. Checking circuit breaker state before attempting operation
    2. Catching OperationalError with lock-related messages
    3. Waiting with exponential backoff: base_delay * (2 ** attempt)
    4. Adding jitter to prevent thundering herd: random ±25% of delay
    5. Recording success/failure for circuit breaker state management
    6. Giving up after max_retries and re-raising the error

    Backoff schedule with defaults (0.05s base, 7 retries):
        Attempt 1: 0.05s, Attempt 2: 0.1s, Attempt 3: 0.2s, Attempt 4: 0.4s,
        Attempt 5: 0.8s, Attempt 6: 1.6s, Attempt 7: 3.2s
        Total max wait: ~6.35s (plus jitter)
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            func_name = getattr(func, "__name__", getattr(func, "__qualname__", "<callable>"))

            # Check circuit breaker state
            if use_circuit_breaker:
                state = get_circuit_state()
                if state == CircuitState.OPEN:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker is open for database operations. "
                        f"Function {func_name} will not be attempted. "
                        f"This typically indicates sustained database lock contention."
                    )

            for attempt in range(max_retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    # Success - reset circuit breaker
                    if use_circuit_breaker and attempt > 0:
                        # Only record success if we had retries (indicates recovery)
                        await _record_circuit_success()
                    return result

                except (OperationalError, SATimeoutError) as e:
                    error_msg = str(e)
                    is_lock = _is_lock_error(error_msg)
                    is_pool = _is_pool_exhausted_error(e)

                    if not (is_lock or is_pool) or attempt >= max_retries:
                        # Not a retryable error, or we've exhausted retries
                        if use_circuit_breaker:
                            await _record_circuit_failure()
                        raise

                    last_exception = e

                    # Calculate exponential backoff with jitter
                    delay = min(base_delay * (2**attempt), max_delay)
                    # Add ±25% jitter to prevent thundering herd
                    jitter = delay * 0.25 * (2 * random.random() - 1)
                    total_delay = max(0.01, delay + jitter)  # Ensure positive delay

                    error_type = "pool_exhausted" if is_pool else "db_locked"
                    _logger.warning(
                        f"db.{error_type}",
                        extra={
                            "function": func_name,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "delay_seconds": round(total_delay, 3),
                            "error": error_msg[:200],
                        },
                    )

                    await asyncio.sleep(total_delay)

            # Should never reach here, but just in case
            if use_circuit_breaker:
                await _record_circuit_failure()
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry loop exit")

        return wrapper

    return decorator


def _build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build async SQLAlchemy engine with SQLite-optimized settings for high-concurrency multi-agent workloads.

    SQLite Concurrency Tuning:
    - WAL mode: Allows concurrent readers + one writer (vs default rollback journal)
    - NORMAL sync: 10x faster than FULL, still durable (WAL provides crash safety)
    - busy_timeout=60s: Extended timeout during checkpoint operations
    - wal_autocheckpoint=1000: Checkpoint every 1000 pages (~4MB) to prevent WAL bloat
    - cache_size=-32768: 32MB page cache for better read performance

    Pool Tuning:
    - Conservative pool size (3 base + 4 overflow = 7 max) to prevent FD exhaustion
    - 45s pool timeout - long enough for checkpoint but not indefinite
    - pool_pre_ping: Detect and recycle stale connections
    """
    from sqlalchemy import event
    from sqlalchemy.engine import make_url

    # For SQLite, enable WAL mode and set timeout for better concurrent access
    connect_args = {}
    is_sqlite = "sqlite" in settings.url.lower()

    if is_sqlite:
        # Ensure parent directory exists for file-backed SQLite URLs.
        # SQLite returns "unable to open database file" when the directory is missing.
        try:
            parsed = make_url(settings.url)
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

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
            "timeout": 60.0,  # Extended timeout (60s) to handle checkpoint stalls
            "check_same_thread": False,  # Required for async SQLite
        }

    # SQLite concurrency tuning:
    # - Smaller pool to prevent FD exhaustion (3 base + 4 overflow = 7 max connections)
    # - Longer timeout to handle WAL checkpoint blocking
    # For non-SQLite (PostgreSQL, etc.), use larger pools
    pool_size = settings.pool_size if settings.pool_size is not None else (3 if is_sqlite else 25)
    max_overflow = settings.max_overflow if settings.max_overflow is not None else (4 if is_sqlite else 25)
    pool_timeout = settings.pool_timeout if settings.pool_timeout is not None else (45 if is_sqlite else 30)

    engine = create_async_engine(
        settings.url,
        echo=settings.echo,
        future=True,
        pool_pre_ping=True,  # Detect and recycle stale connections
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,  # Extended timeout for SQLite checkpoint scenarios
        pool_recycle=600,  # Recycle connections every 10 minutes to prevent stale handle buildup under sustained load
        pool_reset_on_return="rollback",  # Ensure uncommitted transactions are rolled back on return
        connect_args=connect_args,
    )

    # For SQLite: Set up event listener to configure each connection with optimized PRAGMAs
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, connection_record: Any) -> None:
            """Set SQLite PRAGMAs for high-concurrency multi-agent performance.

            These settings are optimized for scenarios with many concurrent agents
            reading and writing to the same database:

            - journal_mode=WAL: Write-Ahead Logging for concurrent reads during writes
            - synchronous=NORMAL: 10x faster than FULL, WAL provides crash safety
            - busy_timeout=60000: 60s wait for locks (handles checkpoint stalls)
            - wal_autocheckpoint=1000: Checkpoint every ~4MB to prevent WAL bloat
            - cache_size=-32768: 32MB page cache (negative = KB, positive = pages)
            - temp_store=MEMORY: Temp tables in memory for faster operations
            - mmap_size=268435456: 256MB memory-mapped I/O for faster reads
            """
            cursor = dbapi_conn.cursor()
            try:
                # Enable WAL mode for concurrent reads/writes
                # This is persistent - only needs to be set once per database file
                cursor.execute("PRAGMA journal_mode=WAL")

                # Use NORMAL synchronous mode (safer than OFF, faster than FULL)
                # With WAL mode, NORMAL provides durability without the FULL penalty
                cursor.execute("PRAGMA synchronous=NORMAL")

                # Extended busy timeout (60 seconds) to handle:
                # - WAL checkpoint blocking (can take seconds with large WAL)
                # - Concurrent write contention from multiple agents
                cursor.execute("PRAGMA busy_timeout=60000")

                # WAL autocheckpoint: checkpoint every 1000 pages (~4MB)
                # Prevents WAL file from growing unbounded while not checkpointing too often
                # Default is 1000, but setting explicitly for documentation
                cursor.execute("PRAGMA wal_autocheckpoint=1000")

                # Larger page cache (32MB) for better read performance
                # Negative value = KB, positive = pages
                cursor.execute("PRAGMA cache_size=-32768")

                # Keep temp tables in memory for faster operations
                cursor.execute("PRAGMA temp_store=MEMORY")

                # Enable memory-mapped I/O for faster reads (256MB limit)
                # This is particularly helpful for read-heavy workloads
                cursor.execute("PRAGMA mmap_size=268435456")

            finally:
                cursor.close()

        @event.listens_for(engine.sync_engine, "checkin")
        def on_checkin(dbapi_conn: Any, connection_record: Any) -> None:
            """Perform passive WAL checkpoint when connection returns to pool.

            PASSIVE checkpoint doesn't block writers - it only checkpoints pages
            that can be checkpointed without waiting. This helps keep WAL size
            manageable without causing lock contention.
            """
            try:
                cursor = dbapi_conn.cursor()
                try:
                    # PASSIVE mode: checkpoint what we can without blocking
                    # Returns (blocked, wal_pages, checkpointed_pages)
                    cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
                finally:
                    cursor.close()
            except Exception:
                # Ignore checkpoint errors - they're non-critical
                pass

    return engine


def install_query_hooks(engine: AsyncEngine) -> None:
    """Install lightweight query counting hooks on the engine (idempotent)."""
    global _QUERY_HOOKS_INSTALLED
    if _QUERY_HOOKS_INSTALLED:
        return
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        tracker = _QUERY_TRACKER.get()
        if tracker is None:
            return
        timings = conn.info.setdefault("query_start_time", [])
        timings.append(time.perf_counter())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        tracker = _QUERY_TRACKER.get()
        if tracker is None:
            return
        timings = conn.info.get("query_start_time")
        if not timings:
            return
        start_time = timings.pop()
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        tracker.record(statement, duration_ms)

    _QUERY_HOOKS_INSTALLED = True


def init_engine(settings: Settings | None = None) -> None:
    """Initialise global engine and session factory once."""
    global _engine, _session_factory
    if _engine is not None and _session_factory is not None:
        return
    resolved_settings = settings or get_settings()
    engine = _build_engine(resolved_settings.database)
    install_query_hooks(engine)
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
async def get_session(*, check_circuit_breaker: bool = False) -> AsyncIterator[AsyncSession]:
    """Provide an async database session with guaranteed cleanup.

    This context manager ensures the session is always closed, even under task
    cancellation. Uses asyncio.shield() to prevent cancellation from interrupting
    the close operation.

    Args:
        check_circuit_breaker: If True, check circuit breaker state before yielding session.
            Raises CircuitBreakerOpenError if circuit is open. Default False for backwards
            compatibility - most callers use retry_on_db_lock which handles this.

    Note: We do NOT call session.rollback() here because that would expire all
    loaded objects, causing DetachedInstanceError when code tries to access
    attributes after the session closes. The pool_reset_on_return='rollback'
    setting handles uncommitted transactions at the pool level instead.
    """
    if check_circuit_breaker:
        state = get_circuit_state()
        if state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                "Circuit breaker is open for database operations. "
                "This typically indicates sustained database lock contention."
            )

    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        # Ensure session close completes even under cancellation (anyio cancel scopes
        # will raise asyncio.CancelledError which is BaseException in Python 3.14).
        close_task = asyncio.create_task(session.close())
        try:
            await asyncio.shield(close_task)
        except BaseException:
            with suppress(BaseException):
                await close_task
            raise


def get_db_health_status() -> dict[str, Any]:
    """Return database health status including circuit breaker state and pool info.

    Returns:
        Dict with circuit_state, pool stats (if available), and recommendations.
    """
    state = get_circuit_state()
    status: dict[str, Any] = {
        "circuit_state": state.value,
        "circuit_failures": _circuit_breaker_failures,
    }

    if _engine is not None:
        pool = _engine.pool
        # Pool attributes are available at runtime but not in type stubs
        status["pool"] = {
            "size": pool.size(),  # type: ignore[attr-defined]
            "checked_in": pool.checkedin(),  # type: ignore[attr-defined]
            "checked_out": pool.checkedout(),  # type: ignore[attr-defined]
            "overflow": pool.overflow(),  # type: ignore[attr-defined]
        }

    if state == CircuitState.OPEN:
        status["recommendation"] = (
            "Circuit breaker is OPEN. Database is experiencing sustained lock contention. "
            "Consider: (1) reducing concurrent operations, (2) increasing busy_timeout, "
            "(3) checking for long-running transactions, (4) running PRAGMA wal_checkpoint(TRUNCATE)."
        )
    elif state == CircuitState.HALF_OPEN:
        status["recommendation"] = (
            "Circuit breaker is HALF_OPEN. Testing if database has recovered. "
            "Next successful operation will reset the circuit."
        )

    return status


@retry_on_db_lock(max_retries=7, base_delay=0.1, max_delay=8.0, use_circuit_breaker=False)
async def ensure_schema(settings: Settings | None = None) -> None:
    """Ensure database schema exists (creates tables from SQLModel definitions).

    This is the pure SQLModel approach:
    - Models define the schema
    - create_all() creates tables that don't exist yet
    - For schema changes: delete the DB and regenerate (dev) or use Alembic (prod)

    Also enables SQLite WAL mode for better concurrent access.

    Note: Circuit breaker is disabled for schema operations since they're
    typically run at startup before the circuit breaker should be active.
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
    # Dispose any existing engine/pool first to avoid leaking file descriptors across tests.
    if _engine is not None:
        engine = _engine
        try:
            # Prefer a full async dispose when possible (aiosqlite uses background threads).
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is not None and running.is_running():
                # Can't block; fall back to sync pool disposal (best effort).
                engine.sync_engine.dispose()
            else:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and not loop.is_running() and not loop.is_closed():
                    loop.run_until_complete(engine.dispose())
                else:
                    asyncio.run(engine.dispose())
        except Exception:
            # Last resort: sync pool disposal.
            with suppress(Exception):
                engine.sync_engine.dispose()
    _engine = None
    _session_factory = None
    _schema_ready = False
    _schema_lock = None
    # Tests frequently mutate env vars; keep settings cache in sync with DB resets.
    clear_settings_cache()


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
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_message_recipients_agent_message "
        "ON message_recipients(agent_id, message_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_project_sender_created "
        "ON messages(project_id, sender_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_project_released_expires "
        "ON file_reservations(project_id, released_ts, expires_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_project_agent_released "
        "ON file_reservations(project_id, agent_id, released_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_product_project "
        "ON product_project_links(product_id, project_id)"
    )
    # AgentLink indexes for efficient contact lookups
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_a_project "
        "ON agent_links(a_project_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_b_project "
        "ON agent_links(b_project_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_b_project_agent "
        "ON agent_links(b_project_id, b_agent_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_status "
        "ON agent_links(status)"
    )
    # Migration: add topic column to messages table (bd-26w broadcast topics)
    with suppress(Exception):
        connection.exec_driver_sql("ALTER TABLE messages ADD COLUMN topic TEXT DEFAULT NULL")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_project_topic ON messages(project_id, topic)"
    )
    # Migration: create message_summaries table (bd-1ia on-demand summarization)
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS message_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            summary_text TEXT NOT NULL,
            start_ts TIMESTAMP NOT NULL,
            end_ts TIMESTAMP NOT NULL,
            source_message_count INTEGER NOT NULL DEFAULT 0,
            source_thread_ids TEXT NOT NULL DEFAULT '[]',
            llm_model TEXT,
            cost_usd REAL,
            created_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_summaries_project_end ON message_summaries(project_id, end_ts)"
    )


def get_database_path(settings: Settings | None = None) -> Path | None:
    """Extract the filesystem path to the SQLite database file from settings.

    Args:
        settings: Application settings, or None to use global settings

    Returns:
        Path to the database file, or None if not using SQLite or path cannot be determined
    """
    resolved = settings or get_settings()
    url_raw = resolved.database.url

    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(url_raw)
    except Exception:
        return None

    if parsed.get_backend_name() != "sqlite":
        return None

    db_path = parsed.database
    if not db_path or db_path == ":memory:":
        return None

    return Path(db_path)
