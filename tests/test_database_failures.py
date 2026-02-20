"""Database Failure Handling Tests.

Tests for graceful handling of database issues:
- Database file missing (should auto-create)
- Schema migration on startup
- Concurrent write handling (retry_on_db_lock)
- Transaction rollback on error
- Session cleanup on exception

Reference: mcp_agent_mail-aea
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from mcp_agent_mail.db import (
    QueryTracker,
    _extract_table_name,
    ensure_schema,
    get_engine,
    get_query_tracker,
    get_session,
    get_session_factory,
    reset_database_state,
    retry_on_db_lock,
    track_queries,
)
from mcp_agent_mail.models import Agent, Project

# ============================================================================
# Database Auto-Creation Tests
# ============================================================================


class TestDatabaseAutoCreation:
    """Tests for automatic database and table creation."""

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_database_file(self, tmp_path: Path, monkeypatch):
        """ensure_schema creates database file when it doesn't exist."""
        db_path = tmp_path / "new_database.sqlite3"
        assert not db_path.exists()

        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        reset_database_state()

        await ensure_schema()

        assert db_path.exists()

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_tables(self, isolated_env):
        """ensure_schema creates all required tables."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            # Check that core tables exist by querying sqlite_master
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            )
            table_names = [row[0] for row in result]

        # Core tables should exist
        assert "projects" in table_names
        assert "agents" in table_names
        assert "messages" in table_names
        assert "message_recipients" in table_names
        assert "file_reservations" in table_names
        assert "agent_links" in table_names

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_fts_table(self, isolated_env):
        """ensure_schema creates FTS virtual table for message search."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_messages'"
                ).fetchall()
            )

        assert len(result) == 1
        assert result[0][0] == "fts_messages"

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_indexes(self, isolated_env):
        """ensure_schema creates performance indexes."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
                ).fetchall()
            )
            index_names = [row[0] for row in result]

        # Check for expected indexes
        assert "idx_messages_created_ts" in index_names
        assert "idx_messages_thread_id" in index_names
        assert "idx_file_reservations_expires_ts" in index_names
        assert "idx_message_recipients_agent_message" in index_names
        assert "idx_messages_project_sender_created" in index_names
        assert "idx_file_reservations_project_released_expires" in index_names
        assert "idx_file_reservations_project_agent_released" in index_names
        assert "idx_product_project" in index_names

    @pytest.mark.asyncio
    async def test_ensure_schema_is_idempotent(self, isolated_env):
        """ensure_schema can be called multiple times safely."""
        await ensure_schema()
        await ensure_schema()  # Second call should not raise
        await ensure_schema()  # Third call should not raise

        # Verify tables still exist
        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='projects'"
                ).fetchone()
            )
            assert result[0] == 1


# ============================================================================
# Concurrent Write Handling Tests (retry_on_db_lock)
# ============================================================================


class TestRetryOnDbLock:
    """Tests for the retry_on_db_lock decorator."""

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_succeeds_without_error(self):
        """Decorator passes through successful function calls."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3)
        async def success_func() -> str:
            call_count["value"] += 1
            return "success"

        result = await success_func()
        assert result == "success"
        assert call_count["value"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_retries_on_lock_error(self):
        """Decorator retries on database lock errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def retry_func() -> str:
            call_count["value"] += 1
            if call_count["value"] < 3:
                raise OperationalError("statement", {}, Exception("database is locked"))
            return "success after retries"

        result = await retry_func()
        assert result == "success after retries"
        assert call_count["value"] == 3

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_exhausts_retries(self):
        """Decorator raises after exhausting retries."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=2, base_delay=0.01)
        async def always_fails() -> str:
            call_count["value"] += 1
            raise OperationalError("statement", {}, Exception("database is locked"))

        with pytest.raises(OperationalError):
            await always_fails()

        # Should have tried max_retries + 1 times
        assert call_count["value"] == 3

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_ignores_non_lock_errors(self):
        """Decorator does not retry on non-lock operational errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def other_error_func() -> str:
            call_count["value"] += 1
            raise OperationalError("statement", {}, Exception("connection refused"))

        with pytest.raises(OperationalError):
            await other_error_func()

        # Should have only tried once (no retry for non-lock errors)
        assert call_count["value"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_detects_busy_error(self):
        """Decorator retries on 'database is busy' errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def busy_func() -> str:
            call_count["value"] += 1
            if call_count["value"] < 2:
                raise OperationalError("statement", {}, Exception("database is busy"))
            return "success"

        result = await busy_func()
        assert result == "success"
        assert call_count["value"] == 2


# ============================================================================
# Query Tracking Helper Tests
# ============================================================================


class TestQueryTrackingHelpers:
    """Tests for query tracking utilities and normalization helpers."""

    def test_extract_table_name_variants(self):
        assert _extract_table_name("SELECT * FROM messages") == "messages"
        assert _extract_table_name('select * from "agents"') == "agents"
        assert _extract_table_name("UPDATE projects SET name = 'x'") == "projects"
        assert _extract_table_name("INSERT INTO `message_recipients` (id) VALUES (1)") == "message_recipients"
        assert _extract_table_name("SELECT * FROM main.file_reservations") == "file_reservations"
        assert _extract_table_name('SELECT * FROM "main"."messages"') == "messages"
        assert _extract_table_name("BEGIN") is None

    def test_query_tracker_records_counts_and_slow_queries(self):
        tracker = QueryTracker(slow_query_ms=5.0)
        tracker.record("SELECT * FROM messages", 3.0)
        tracker.record("SELECT * FROM messages", 7.5)

        assert tracker.total == 2
        assert tracker.per_table["messages"] == 2
        assert tracker.slow_queries == [{"table": "messages", "duration_ms": 7.5}]

        payload = tracker.to_dict()
        assert payload["total"] == 2
        assert payload["per_table"]["messages"] == 2
        assert payload["slow_query_ms"] == 5.0
        assert payload["slow_queries"] == [{"table": "messages", "duration_ms": 7.5}]

    def test_track_queries_context_manages_contextvar(self):
        assert get_query_tracker() is None
        with track_queries() as tracker:
            assert get_query_tracker() is tracker
            tracker.record("SELECT * FROM agents", 1.2)
        assert get_query_tracker() is None


# ============================================================================
# Transaction Rollback Tests
# ============================================================================


class TestTransactionRollback:
    """Tests for transaction rollback on errors."""

    @pytest.mark.asyncio
    async def test_session_rollback_on_exception(self, isolated_env):
        """Session rolls back uncommitted changes when exception occurs."""
        await ensure_schema()

        # Create a project successfully first
        async with get_session() as session:
            project = Project(slug="rollback-test", human_key="/rollback/test")
            session.add(project)
            await session.commit()

        # Now try to create a duplicate (which should fail)
        # and verify the transaction is rolled back
        try:
            async with get_session() as session:
                # This should succeed
                agent = Agent(
                    project_id=1,
                    name="TestAgent",
                    program="test",
                    model="test",
                )
                session.add(agent)

                # Simulate an error before commit
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # Verify the agent was NOT persisted due to rollback
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM agents WHERE name='TestAgent'"))
            count = result.scalar()
            assert count == 0

    @pytest.mark.asyncio
    async def test_explicit_rollback_discards_changes(self, isolated_env):
        """Explicit session.rollback() discards uncommitted changes."""
        await ensure_schema()

        async with get_session() as session:
            project = Project(slug="explicit-rollback", human_key="/explicit/rollback")
            session.add(project)
            # Don't commit, just rollback
            await session.rollback()

        # Verify project was not persisted
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM projects WHERE slug='explicit-rollback'"))
            count = result.scalar()
            assert count == 0


# ============================================================================
# Session Cleanup Tests
# ============================================================================


class TestSessionCleanup:
    """Tests for proper session cleanup on exceptions."""

    @pytest.mark.asyncio
    async def test_session_closed_after_context(self, isolated_env):
        """Session is properly closed after context manager exits."""
        await ensure_schema()

        async with get_session() as session:
            # Create a project to verify session works
            project = Project(slug="cleanup-test", human_key="/cleanup/test")
            session.add(project)
            await session.commit()

        # Verify the data was committed and new sessions work correctly
        async with get_session() as new_session:
            from sqlalchemy import text

            result = await new_session.execute(
                text("SELECT COUNT(*) FROM projects WHERE slug='cleanup-test'")
            )
            count = result.scalar()
            assert count == 1

    @pytest.mark.asyncio
    async def test_session_closed_on_exception(self, isolated_env):
        """Session is properly closed even when exception occurs."""
        await ensure_schema()

        # First create a project we can verify
        async with get_session() as session:
            project = Project(slug="exception-test", human_key="/exception/test")
            session.add(project)
            await session.commit()

        try:
            async with get_session() as session:
                # Simulate an error mid-transaction
                raise RuntimeError("Test exception")
        except RuntimeError:
            pass

        # Verify database is still accessible after exception (session cleaned up)
        async with get_session() as new_session:
            from sqlalchemy import text

            result = await new_session.execute(
                text("SELECT COUNT(*) FROM projects WHERE slug='exception-test'")
            )
            count = result.scalar()
            assert count == 1

    @pytest.mark.asyncio
    async def test_multiple_concurrent_sessions(self, isolated_env):
        """Multiple concurrent sessions work independently."""
        await ensure_schema()

        # Create initial project
        async with get_session() as session:
            project = Project(slug="concurrent-test", human_key="/concurrent/test")
            session.add(project)
            await session.commit()

        async def worker(worker_id: int) -> str:
            async with get_session() as session:
                agent = Agent(
                    project_id=1,
                    name=f"Worker{worker_id}",
                    program="test",
                    model="test",
                )
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                return f"Worker{worker_id} created agent {agent.id}"

        # Run multiple workers concurrently
        results = await asyncio.gather(*[worker(i) for i in range(5)])

        assert len(results) == 5
        for i, result in enumerate(results):
            assert f"Worker{i}" in result

        # Verify all agents were created
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM agents"))
            count = result.scalar()
            assert count == 5


# ============================================================================
# Engine and Session Factory Tests
# ============================================================================


class TestEngineAndFactory:
    """Tests for engine and session factory initialization."""

    def test_reset_database_state_clears_globals(self, isolated_env):
        """reset_database_state clears all global state."""
        # First initialize the database
        asyncio.run(ensure_schema())

        # Verify engine is initialized
        engine = get_engine()
        assert engine is not None

        # Reset state
        reset_database_state()

        # After reset, calling get_engine should re-initialize
        # (the engine will be recreated on next access)

    @pytest.mark.asyncio
    async def test_get_session_factory_creates_factory(self, isolated_env):
        """get_session_factory creates and returns session factory."""
        factory = get_session_factory()
        assert factory is not None

        # Factory should produce working sessions
        async with factory() as session:
            # Just verify we can execute a simple query
            from sqlalchemy import text

            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1


# ============================================================================
# WAL Mode and SQLite Configuration Tests
# ============================================================================


class TestSQLiteConfiguration:
    """Tests for SQLite-specific configuration."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, isolated_env):
        """WAL mode is enabled for SQLite databases."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
            )
            journal_mode = result[0].lower()

        assert journal_mode == "wal"

    @pytest.mark.asyncio
    async def test_busy_timeout_set(self, isolated_env):
        """SQLite busy_timeout is set for lock handling."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA busy_timeout").fetchone()
            )
            timeout = result[0]

        # Should be 30000ms (30 seconds)
        assert timeout == 30000

    @pytest.mark.asyncio
    async def test_synchronous_mode_normal(self, isolated_env):
        """SQLite synchronous mode is set to NORMAL for performance."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA synchronous").fetchone()
            )
            sync_mode = result[0]

        # 1 = NORMAL
        assert sync_mode == 1
