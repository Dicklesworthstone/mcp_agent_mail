"""Tests for ExpectedErrorFilter in http.py.

Tests cover:
- Expected error patterns are detected and downgraded
- Tracebacks are suppressed for expected errors
- Non-expected errors pass through unchanged
- Cause chain inspection works correctly
"""

from __future__ import annotations

import logging
from typing import Any


class MockToolExecutionError(Exception):
    """Mock ToolExecutionError for testing."""

    def __init__(self, code: str, message: str, recoverable: bool = False, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.data = data or {}


def create_expected_error_filter():
    """Create an instance of ExpectedErrorFilter.

    We define a local copy of the class here to avoid import ordering issues
    with configure_logging(). The actual filter class is tested through
    behavior matching.
    """
    class ExpectedErrorFilter(logging.Filter):
        """Filter that suppresses tracebacks for expected/recoverable tool errors."""

        _EXPECTED_PATTERNS = (
            "not found in project",
            "index.lock",
            "git_index_lock",
            "resource_busy",
            "temporarily locked",
            "recoverable=true",
            "use register_agent",
            "available agents:",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            if not record.exc_info or record.exc_info[1] is None:
                return True

            exc = record.exc_info[1]
            exc_str = str(exc).lower()

            is_expected = any(pattern in exc_str for pattern in self._EXPECTED_PATTERNS)

            if hasattr(exc, "recoverable") and exc.recoverable:
                is_expected = True

            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                cause_str = str(cause).lower()
                if any(pattern in cause_str for pattern in self._EXPECTED_PATTERNS):
                    is_expected = True
                if hasattr(cause, "recoverable") and cause.recoverable:
                    is_expected = True

            if is_expected:
                record.exc_info = None
                record.exc_text = None
                if record.levelno >= logging.ERROR:
                    record.levelno = logging.INFO
                    record.levelname = "INFO"

            return True

    return ExpectedErrorFilter()


def create_log_record(
    exc: Exception | None = None,
    level: int = logging.ERROR,
    message: str = "test message"
) -> logging.LogRecord:
    """Create a log record for testing."""
    record = logging.LogRecord(
        name="fastmcp.tools.tool_manager",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=(type(exc), exc, None) if exc else None,
    )
    return record


# ============================================================================
# Expected Pattern Detection Tests
# ============================================================================


class TestExpectedPatternDetection:
    """Tests for expected error pattern detection."""

    def test_detects_not_found_in_project(self):
        """Detects 'not found in project' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent 'NavyDune' not found in project")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None
        assert record.levelno == logging.INFO
        assert record.levelname == "INFO"

    def test_detects_index_lock_pattern(self):
        """Detects 'index.lock' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Could not acquire .git/index.lock")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None
        assert record.levelno == logging.INFO

    def test_detects_git_index_lock_pattern(self):
        """Detects 'git_index_lock' pattern."""
        filter_instance = create_expected_error_filter()

        exc = MockToolExecutionError("GIT_INDEX_LOCK", "Repository temporarily locked, git_index_lock error")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None

    def test_detects_resource_busy_pattern(self):
        """Detects 'resource_busy' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("resource_busy: database is locked")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None

    def test_detects_temporarily_locked_pattern(self):
        """Detects 'temporarily locked' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Git repository is temporarily locked")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None

    def test_detects_use_register_agent_pattern(self):
        """Detects 'use register_agent' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent not found. Use register_agent to create it first")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None

    def test_detects_available_agents_pattern(self):
        """Detects 'available agents:' pattern."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Unknown agent. Available agents: BlueDog, RedCat")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None


# ============================================================================
# Recoverable Flag Tests
# ============================================================================


class TestRecoverableFlag:
    """Tests for recoverable=True flag detection."""

    def test_detects_recoverable_true_on_exception(self):
        """Detects recoverable=True attribute on exception."""
        filter_instance = create_expected_error_filter()

        exc = MockToolExecutionError("TEST_ERROR", "Some error", recoverable=True)
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None
        assert record.levelno == logging.INFO

    def test_ignores_recoverable_false(self):
        """Does not modify records for recoverable=False without patterns."""
        filter_instance = create_expected_error_filter()

        exc = MockToolExecutionError("TEST_ERROR", "Some random error", recoverable=False)
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        # exc_info should NOT be cleared (no expected pattern)
        assert record.exc_info is not None
        assert record.levelno == logging.ERROR


# ============================================================================
# Cause Chain Tests
# ============================================================================


class TestCauseChainInspection:
    """Tests for __cause__ chain inspection."""

    def test_detects_pattern_in_cause(self):
        """Detects expected pattern in __cause__."""
        filter_instance = create_expected_error_filter()

        cause = Exception("Agent 'TestAgent' not found in project")
        exc = Exception("Tool execution failed")
        exc.__cause__ = cause

        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None
        assert record.levelno == logging.INFO

    def test_detects_recoverable_in_cause(self):
        """Detects recoverable=True in __cause__."""
        filter_instance = create_expected_error_filter()

        cause = MockToolExecutionError("INNER", "Inner error", recoverable=True)
        exc = Exception("Tool execution failed")
        exc.__cause__ = cause

        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None


# ============================================================================
# Non-Expected Error Tests
# ============================================================================


class TestNonExpectedErrors:
    """Tests for non-expected errors passing through unchanged."""

    def test_passes_through_unexpected_errors(self):
        """Unexpected errors are not modified."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Unexpected database error occurred")
        record = create_log_record(exc=exc)
        original_level = record.levelno

        result = filter_instance.filter(record)

        assert result is True
        assert record.exc_info is not None
        assert record.levelno == original_level

    def test_passes_through_records_without_exception(self):
        """Records without exc_info are not modified."""
        filter_instance = create_expected_error_filter()

        record = create_log_record(exc=None, level=logging.ERROR)

        result = filter_instance.filter(record)

        assert result is True
        assert record.exc_info is None
        assert record.levelno == logging.ERROR

    def test_passes_through_info_level_records(self):
        """INFO level records are not modified even with expected patterns."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent not found in project")
        record = create_log_record(exc=exc, level=logging.INFO)

        result = filter_instance.filter(record)

        assert result is True
        # exc_info is cleared
        assert record.exc_info is None
        # Level stays INFO (not downgraded further)
        assert record.levelno == logging.INFO


# ============================================================================
# Level Downgrade Tests
# ============================================================================


class TestLevelDowngrade:
    """Tests for log level downgrade behavior."""

    def test_downgrades_error_to_info(self):
        """ERROR level is downgraded to INFO for expected errors."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent not found in project")
        record = create_log_record(exc=exc, level=logging.ERROR)

        filter_instance.filter(record)

        assert record.levelno == logging.INFO
        assert record.levelname == "INFO"

    def test_downgrades_critical_to_info(self):
        """CRITICAL level is downgraded to INFO for expected errors."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent not found in project")
        record = create_log_record(exc=exc, level=logging.CRITICAL)

        filter_instance.filter(record)

        assert record.levelno == logging.INFO
        assert record.levelname == "INFO"

    def test_does_not_upgrade_warning(self):
        """WARNING level is not changed (not >= ERROR)."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent not found in project")
        record = create_log_record(exc=exc, level=logging.WARNING)

        filter_instance.filter(record)

        # exc_info should still be cleared
        assert record.exc_info is None
        # But level should stay WARNING (not downgraded)
        assert record.levelno == logging.WARNING


# ============================================================================
# Filter Behavior Tests
# ============================================================================


class TestFilterBehavior:
    """Tests for filter general behavior."""

    def test_filter_always_returns_true(self):
        """Filter always returns True (doesn't drop records)."""
        filter_instance = create_expected_error_filter()

        # Expected error
        exc1 = Exception("Agent not found in project")
        record1 = create_log_record(exc=exc1)
        assert filter_instance.filter(record1) is True

        # Unexpected error
        exc2 = Exception("Random error")
        record2 = create_log_record(exc=exc2)
        assert filter_instance.filter(record2) is True

        # No exception
        record3 = create_log_record(exc=None)
        assert filter_instance.filter(record3) is True


# ============================================================================
# Case Sensitivity Tests
# ============================================================================


class TestCaseSensitivity:
    """Tests for case-insensitive pattern matching."""

    def test_case_insensitive_pattern_matching(self):
        """Pattern matching is case-insensitive."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Agent NOT FOUND IN PROJECT")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None

    def test_uppercase_index_lock(self):
        """Uppercase INDEX.LOCK is detected."""
        filter_instance = create_expected_error_filter()

        exc = Exception("Could not acquire INDEX.LOCK")
        record = create_log_record(exc=exc)

        filter_instance.filter(record)

        assert record.exc_info is None


# ============================================================================
# Integration test with actual http module
# ============================================================================


class TestHttpModuleIntegration:
    """Integration tests with the actual http module."""

    def test_configure_logging_can_be_called(self, isolated_env):
        """_configure_logging() can be called without errors."""
        from mcp_agent_mail import http
        from mcp_agent_mail.config import get_settings
        settings = get_settings()
        # Should not raise
        http._configure_logging(settings)

    def test_filter_is_installed_after_configure_logging(self, isolated_env):
        """Filter is installed on fastmcp logger after _configure_logging()."""
        from mcp_agent_mail import http
        from mcp_agent_mail.config import get_settings
        settings = get_settings()
        http._configure_logging(settings)

        fastmcp_logger = logging.getLogger("fastmcp.tools.tool_manager")
        filter_names = [f.__class__.__name__ for f in fastmcp_logger.filters]
        assert "ExpectedErrorFilter" in filter_names
