"""CI Performance Regression Detection Tests.

This test module is designed to run in CI to catch performance regressions.
It compares current benchmark results against baseline thresholds defined in
baselines.json.

Reference: mcp_agent_mail-tty (CI performance regression detection task)

Usage:
    # Run regression checks (uses existing benchmark results)
    pytest tests/benchmarks/test_ci_regression.py -v

    # Run with benchmarks first (generates fresh results)
    RUN_BENCHMARKS=1 pytest tests/benchmarks/ -v -m "benchmark or ci_regression"

Environment variables:
    CI_REGRESSION_STRICT: Set to "1" to fail on warnings too (default: errors only)
    CI_REGRESSION_SKIP: Set to "1" to skip regression checks entirely
"""

from __future__ import annotations

import os

import pytest

from .regression import (
    assert_no_regressions,
    assert_query_count_lte,
    check_regressions,
    load_baselines,
    render_report,
    write_report_json,
)


def _should_skip_regression() -> bool:
    """Check if regression checks should be skipped."""
    return os.getenv("CI_REGRESSION_SKIP") == "1"


def _is_strict_mode() -> bool:
    """Check if strict mode is enabled (fail on warnings)."""
    return os.getenv("CI_REGRESSION_STRICT") == "1"


@pytest.mark.ci_regression
def test_performance_regression_check():
    """Main CI regression check - fails if thresholds exceeded.

    This test:
    1. Loads baseline thresholds from baselines.json
    2. Loads latest benchmark results from tests/benchmarks/results/
    3. Compares each metric against thresholds
    4. Renders a Rich report to stdout for CI logs
    5. Writes JSON artifact for CI download
    6. Fails if any violation (error) is found
    """
    if _should_skip_regression():
        pytest.skip("Skipped by CI_REGRESSION_SKIP=1")

    # This will raise AssertionError on violations
    assert_no_regressions()


@pytest.mark.ci_regression
def test_baselines_file_valid():
    """Validate that baselines.json is well-formed and has required fields."""
    baselines = load_baselines()

    assert "version" in baselines, "baselines.json missing 'version' field"
    assert "thresholds" in baselines, "baselines.json missing 'thresholds' field"
    assert "baselines" in baselines, "baselines.json missing 'baselines' field"

    # Check that baseline configs have required fields
    for tool, config in baselines.get("baselines", {}).items():
        assert "latency_ms" in config, f"Baseline for {tool} missing 'latency_ms'"
        assert "p95_max" in config["latency_ms"], f"Baseline for {tool} missing 'latency_ms.p95_max'"


@pytest.mark.ci_regression
def test_regression_report_json_artifact():
    """Test that regression report JSON artifact is properly generated."""
    if _should_skip_regression():
        pytest.skip("Skipped by CI_REGRESSION_SKIP=1")

    report = check_regressions()
    json_path = write_report_json(report)

    assert json_path.exists(), f"JSON artifact not created at {json_path}"

    import json

    content = json.loads(json_path.read_text())
    assert "status" in content
    assert "violations" in content
    assert "warnings" in content
    assert "summary" in content


@pytest.mark.ci_regression
def test_regression_with_strict_mode():
    """Test regression check in strict mode (fails on warnings too).

    Only runs if CI_REGRESSION_STRICT=1 is set.
    """
    if not _is_strict_mode():
        pytest.skip("Strict mode not enabled (set CI_REGRESSION_STRICT=1)")

    if _should_skip_regression():
        pytest.skip("Skipped by CI_REGRESSION_SKIP=1")

    report = check_regressions()
    render_report(report)
    write_report_json(report)

    if report.has_warnings or report.has_errors:
        raise AssertionError(
            f"Strict mode: {len(report.violations)} error(s), {len(report.warnings)} warning(s) found"
        )


# Query count assertion tests
class TestQueryCountAssertions:
    """Test query count assertion utilities."""

    def test_query_count_assertion_passes_under_threshold(self):
        """Query count assertion should pass when under threshold."""
        # Simulate a passing check
        assert_query_count_lte("test_op", max_queries=5, actual=3)

    def test_query_count_assertion_fails_over_threshold(self):
        """Query count assertion should fail when over threshold."""
        with pytest.raises(AssertionError) as exc_info:
            assert_query_count_lte("test_op", max_queries=2, actual=5)

        assert "test_op" in str(exc_info.value)
        assert "expected <= 2" in str(exc_info.value)
        assert "got 5" in str(exc_info.value)


# Integration tests that run actual benchmarks with query counting
class TestQueryCountIntegration:
    """Integration tests for query counting during actual operations.

    These tests run actual MCP tool calls and verify query counts
    stay within expected bounds.
    """

    @pytest.mark.asyncio
    @pytest.mark.ci_regression
    async def test_send_message_query_count(self, isolated_env):
        """Verify send_message stays within query count threshold."""
        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.db import track_queries

        server = build_mcp_server()
        max_queries = 5  # From baselines.json

        async with Client(server) as client:
            project_key = "/ci-query-test-send"
            await client.call_tool("ensure_project", {"human_key": project_key})

            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "ci-test",
                    "model": "test",
                },
            )
            agent_name = agent_result.data.get("name")

            # Track queries during send_message
            with track_queries() as tracker:
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": "CI Query Count Test",
                        "body_md": "Testing query count for CI regression.",
                    },
                )

            assert_query_count_lte("send_message", max_queries, tracker.total)

    @pytest.mark.asyncio
    @pytest.mark.ci_regression
    async def test_fetch_inbox_query_count(self, isolated_env):
        """Verify fetch_inbox stays within query count threshold."""
        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.db import track_queries

        server = build_mcp_server()
        max_queries = 3  # From baselines.json

        async with Client(server) as client:
            project_key = "/ci-query-test-inbox"
            await client.call_tool("ensure_project", {"human_key": project_key})

            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "ci-test",
                    "model": "test",
                },
            )
            agent_name = agent_result.data.get("name")

            # Send a few messages first
            for i in range(5):
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Message {i}",
                        "body_md": f"Content {i}",
                    },
                )

            # Track queries during fetch_inbox
            with track_queries() as tracker:
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                        "limit": 20,
                    },
                )

            assert_query_count_lte("fetch_inbox", max_queries, tracker.total)

    @pytest.mark.asyncio
    @pytest.mark.ci_regression
    async def test_list_outbox_query_count(self, isolated_env):
        """Verify list_outbox stays within query count threshold."""
        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.db import track_queries

        server = build_mcp_server()
        max_queries = 3  # From baselines.json

        async with Client(server) as client:
            project_key = "/ci-query-test-outbox"
            await client.call_tool("ensure_project", {"human_key": project_key})

            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "ci-test",
                    "model": "test",
                },
            )
            agent_name = agent_result.data.get("name")

            # Send a few messages first
            for i in range(5):
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Outbox test {i}",
                        "body_md": f"Content {i}",
                    },
                )

            # Track queries during mailbox (list_outbox equivalent via resource)
            with track_queries() as tracker:
                resource_uri = f"resource://outbox/{agent_name}?project={project_key}&limit=20"
                await client.read_resource(resource_uri)

            assert_query_count_lte("list_outbox", max_queries, tracker.total)
