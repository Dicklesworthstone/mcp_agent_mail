"""Async database engine and session management utilities."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .config import DatabaseSettings, Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_schema_ready = False
_schema_lock: asyncio.Lock | None = None


def _build_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.url,
        echo=settings.echo,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )


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


async def run_migrations(apply_fn: Callable[[AsyncEngine], Any] | None = None) -> None:
    """Create all tables using SQLModel metadata and optional custom hook."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_setup_fts)
    if apply_fn:
        result = apply_fn(engine)
        if inspect.isawaitable(result):
            await result


async def ensure_schema(settings: Settings | None = None) -> None:
    """Ensure database schema exists exactly once."""
    global _schema_ready, _schema_lock
    if _schema_ready:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ready:
            return
        init_engine(settings)
        await run_migrations()
        _schema_ready = True


def reset_database_state() -> None:
    """Test helper to reset global engine/session state."""
    global _engine, _session_factory, _schema_ready, _schema_lock
    _engine = None
    _session_factory = None
    _schema_ready = False
    _schema_lock = None


def _setup_fts(connection) -> None:
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
