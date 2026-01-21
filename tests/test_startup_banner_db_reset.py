"""Tests for database state reset after startup banner.

Tests cover:
- _get_database_stats() initializes the global engine
- reset_database_state() clears the engine
- The serve-http flow properly resets state after banner display
"""

from __future__ import annotations

import pytest


class TestStartupBannerDatabaseReset:
    """Tests for the startup banner database reset pattern."""

    def test_get_database_stats_initializes_engine(self, isolated_env):
        """_get_database_stats() initializes the global engine via get_session()."""
        from mcp_agent_mail import db
        from mcp_agent_mail.rich_logger import _get_database_stats

        # Ensure engine is None before calling
        assert db._engine is None

        # Call _get_database_stats which uses asyncio.run() internally
        stats = _get_database_stats()

        # After the call, engine should be initialized
        assert db._engine is not None
        # Stats should be returned (may be zeros if DB is empty)
        assert isinstance(stats, dict)
        assert "projects" in stats

    def test_reset_database_state_clears_engine(self, isolated_env):
        """reset_database_state() clears the global engine."""
        from mcp_agent_mail import db
        from mcp_agent_mail.rich_logger import _get_database_stats

        # Initialize engine via stats
        _get_database_stats()
        assert db._engine is not None

        # Reset should clear it
        db.reset_database_state()
        assert db._engine is None

    def test_serve_http_resets_after_banner(self, isolated_env, monkeypatch):
        """serve_http resets database state after displaying startup banner.

        This prevents connections created on the temporary asyncio.run() event loop
        from being orphaned when uvicorn starts with its own event loop.
        """
        from mcp_agent_mail import db
        from mcp_agent_mail.cli import serve_http

        # Track what happens during serve_http
        banner_displayed = False
        reset_called_after_banner = False
        uvicorn_started = False
        engine_was_none_before_uvicorn = False

        original_reset = db.reset_database_state

        from mcp_agent_mail import rich_logger
        original_display_banner = rich_logger.display_startup_banner

        def mock_display_banner(*args, **kwargs):
            nonlocal banner_displayed
            banner_displayed = True
            # Actually call the real function to initialize engine
            original_display_banner(*args, **kwargs)

        def mock_reset():
            nonlocal reset_called_after_banner
            if banner_displayed and not uvicorn_started:
                reset_called_after_banner = True
            original_reset()

        def mock_uvicorn_run(*args, **kwargs):
            nonlocal uvicorn_started, engine_was_none_before_uvicorn
            engine_was_none_before_uvicorn = db._engine is None
            uvicorn_started = True
            # Don't actually start uvicorn

        import uvicorn as uvicorn_module


        # Apply mocks
        monkeypatch.setattr(rich_logger, "display_startup_banner", mock_display_banner)
        monkeypatch.setattr("mcp_agent_mail.cli.reset_database_state", mock_reset)
        monkeypatch.setattr(uvicorn_module, "run", mock_uvicorn_run)

        # Run serve_http
        serve_http()

        # Verify the sequence
        assert banner_displayed, "Banner should be displayed"
        assert reset_called_after_banner, "reset_database_state should be called after banner"
        assert uvicorn_started, "uvicorn should be started"
        assert engine_was_none_before_uvicorn, "Engine should be None before uvicorn starts"


class TestMultipleEventLoopIsolation:
    """Tests verifying connections are properly isolated across event loops."""

    def test_asyncio_run_creates_connections_on_temporary_loop(self, isolated_env):
        """asyncio.run() creates connections on a temporary event loop.

        This test demonstrates why reset_database_state() is needed after
        using asyncio.run() with database operations.
        """
        from mcp_agent_mail import db
        from mcp_agent_mail.rich_logger import _get_database_stats

        # Start with no engine
        assert db._engine is None

        # First asyncio.run() call creates engine
        _get_database_stats()
        engine_after_first = db._engine
        assert engine_after_first is not None

        # Engine persists across calls (it's global)
        _get_database_stats()
        assert db._engine is engine_after_first

        # Reset clears it
        db.reset_database_state()
        assert db._engine is None

        # Next call creates a fresh engine
        _get_database_stats()
        engine_after_reset = db._engine
        assert engine_after_reset is not None
        # Note: Can't compare identity because the old engine was disposed

    @pytest.mark.asyncio
    async def test_fresh_engine_after_reset_works_correctly(self, isolated_env):
        """Fresh engine created after reset works correctly in async context."""
        from mcp_agent_mail import db

        # Simulate the startup banner scenario:
        # 1. Banner uses asyncio.run() to get stats (in sync context)
        # This test runs in async context, so we simulate by doing the operations

        # First, manually init and reset (simulating what happens at startup)
        db.init_engine()
        old_engine = db._engine
        db.reset_database_state()
        assert db._engine is None

        # Now use the session in async context (like uvicorn would)
        async with db.get_session() as session:
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1"))
            row = result.fetchone()
            assert row is not None
            assert row[0] == 1

        # Engine should now be fresh
        new_engine = db._engine
        assert new_engine is not None
        # Should be a different engine instance
        assert new_engine is not old_engine
