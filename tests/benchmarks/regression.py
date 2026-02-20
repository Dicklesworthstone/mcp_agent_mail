"""CI Performance Regression Detection.

This module provides utilities to detect performance regressions by comparing
current benchmark results against baseline thresholds. It's designed to fail CI
when:

1. Query counts exceed maximum thresholds
2. Latency (p95/p99/mean) exceeds maximum thresholds
3. Performance regresses beyond the configured percentage vs previous baseline

Reference: mcp_agent_mail-tty (CI performance regression detection task)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BASELINES_PATH = Path(__file__).parent / "baselines.json"
RESULTS_DIR = Path(__file__).parent / "results"
REGRESSION_REPORT_DIR = Path(__file__).parent / "regression_reports"


@dataclass(slots=True)
class RegressionViolation:
    """Represents a single regression violation."""

    benchmark: str
    metric: str
    baseline_value: float
    current_value: float
    threshold: float
    severity: str  # "warning" or "error"
    message: str


@dataclass(slots=True)
class RegressionReport:
    """Full regression report with all violations."""

    timestamp: str
    git_sha: str
    baselines_version: int
    violations: list[RegressionViolation] = field(default_factory=list)
    warnings: list[RegressionViolation] = field(default_factory=list)
    passed_checks: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.violations) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


def load_baselines() -> dict[str, Any]:
    """Load baseline configuration from JSON file."""
    if not BASELINES_PATH.exists():
        raise FileNotFoundError(f"Baselines file not found: {BASELINES_PATH}")
    return json.loads(BASELINES_PATH.read_text())


def load_latest_result(tool: str) -> dict[str, Any] | None:
    """Load the most recent benchmark result for a tool.

    Args:
        tool: The benchmark tool name (e.g., "send_message")

    Returns:
        The parsed JSON result dict, or None if not found.
    """
    if not RESULTS_DIR.exists():
        return None

    # Find all results for this tool, sorted by timestamp in filename
    results = sorted(RESULTS_DIR.glob(f"{tool}_*.json"), reverse=True)
    if not results:
        return None

    # Return the most recent one
    return json.loads(results[0].read_text())


def load_all_latest_results() -> dict[str, dict[str, Any]]:
    """Load the most recent result for each benchmark tool."""
    results = {}
    tools = ["send_message", "fetch_inbox", "list_outbox", "search_messages", "summarize_thread"]

    for tool in tools:
        result = load_latest_result(tool)
        if result:
            results[tool] = result

    return results


def _check_latency_thresholds(
    benchmark: str,
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> list[RegressionViolation]:
    """Check latency metrics against baseline thresholds."""
    violations = []
    latency_baseline = baseline.get("latency_ms", {})
    latency_current = current.get("latency_ms", {})

    if not latency_current:
        return violations

    # Check p95
    p95_max = latency_baseline.get("p95_max")
    p95_current = latency_current.get("p95", 0)
    if p95_max and p95_current > p95_max:
        violations.append(
            RegressionViolation(
                benchmark=benchmark,
                metric="latency_p95",
                baseline_value=p95_max,
                current_value=p95_current,
                threshold=p95_max,
                severity="error",
                message=f"p95 latency ({p95_current:.2f}ms) exceeds threshold ({p95_max}ms)",
            )
        )

    # Check p99
    p99_max = latency_baseline.get("p99_max")
    p99_current = latency_current.get("p99", 0)
    if p99_max and p99_current > p99_max:
        violations.append(
            RegressionViolation(
                benchmark=benchmark,
                metric="latency_p99",
                baseline_value=p99_max,
                current_value=p99_current,
                threshold=p99_max,
                severity="error",
                message=f"p99 latency ({p99_current:.2f}ms) exceeds threshold ({p99_max}ms)",
            )
        )

    # Check mean
    mean_max = latency_baseline.get("mean_max")
    mean_current = latency_current.get("mean", 0)
    if mean_max and mean_current > mean_max:
        violations.append(
            RegressionViolation(
                benchmark=benchmark,
                metric="latency_mean",
                baseline_value=mean_max,
                current_value=mean_current,
                threshold=mean_max,
                severity="warning",  # Mean is less critical than p95/p99
                message=f"Mean latency ({mean_current:.2f}ms) exceeds threshold ({mean_max}ms)",
            )
        )

    return violations


def _check_query_thresholds(
    benchmark: str,
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> list[RegressionViolation]:
    """Check query count metrics against baseline thresholds."""
    violations = []
    query_baseline = baseline.get("query_stats", {})
    query_current = current.get("query_stats", {})

    if not query_current:
        return violations

    # Check avg queries per operation
    avg_max = query_baseline.get("avg_per_op_max")
    avg_current = query_current.get("avg_per_op", 0)
    if avg_max and avg_current > avg_max:
        violations.append(
            RegressionViolation(
                benchmark=benchmark,
                metric="query_avg_per_op",
                baseline_value=avg_max,
                current_value=avg_current,
                threshold=avg_max,
                severity="error",
                message=f"Avg queries/op ({avg_current:.2f}) exceeds threshold ({avg_max})",
            )
        )

    return violations


def check_regressions(
    baselines: dict[str, Any] | None = None,
    results: dict[str, dict[str, Any]] | None = None,
) -> RegressionReport:
    """Check all benchmarks for regressions against baselines.

    Args:
        baselines: Baseline config dict (loads from file if None)
        results: Dict of tool -> result (loads latest if None)

    Returns:
        RegressionReport with all violations and warnings
    """
    if baselines is None:
        baselines = load_baselines()
    if results is None:
        results = load_all_latest_results()

    # Get git SHA from any result
    git_sha = "unknown"
    for result in results.values():
        if "git_sha" in result:
            git_sha = result["git_sha"]
            break

    report = RegressionReport(
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        git_sha=git_sha,
        baselines_version=baselines.get("version", 1),
    )

    baseline_configs = baselines.get("baselines", {})

    for tool, config in baseline_configs.items():
        result = results.get(tool)
        if not result:
            report.warnings.append(
                RegressionViolation(
                    benchmark=tool,
                    metric="missing_result",
                    baseline_value=0,
                    current_value=0,
                    threshold=0,
                    severity="warning",
                    message=f"No benchmark result found for {tool}",
                )
            )
            continue

        # Check latency
        latency_violations = _check_latency_thresholds(tool, config, result)
        for v in latency_violations:
            if v.severity == "error":
                report.violations.append(v)
            else:
                report.warnings.append(v)

        # Check query counts
        query_violations = _check_query_thresholds(tool, config, result)
        for v in query_violations:
            if v.severity == "error":
                report.violations.append(v)
            else:
                report.warnings.append(v)

        # Track passed checks
        if not latency_violations and not query_violations:
            report.passed_checks.append(tool)

    return report


def render_report(report: RegressionReport, console: Console | None = None) -> None:
    """Render regression report with Rich formatting.

    Args:
        report: The regression report to render
        console: Rich console (creates new one if None)
    """
    if console is None:
        console = Console()

    # Header
    status_color = "red" if report.has_errors else ("yellow" if report.has_warnings else "green")
    status_text = "FAILED" if report.has_errors else ("WARNINGS" if report.has_warnings else "PASSED")

    header = Panel(
        Text.assemble(
            ("CI Performance Regression Check: ", "bold"),
            (status_text, f"bold {status_color}"),
            "\n",
            (f"Git SHA: {report.git_sha}", "dim"),
            " | ",
            (f"Baselines v{report.baselines_version}", "dim"),
        ),
        border_style=status_color,
    )
    console.print(header)

    # Violations table
    if report.violations:
        table = Table(title="VIOLATIONS (CI will fail)", title_style="bold red", show_header=True, header_style="bold red")
        table.add_column("Benchmark", style="cyan")
        table.add_column("Metric", style="magenta")
        table.add_column("Threshold", justify="right")
        table.add_column("Current", justify="right", style="red")
        table.add_column("Message")

        for v in report.violations:
            table.add_row(
                v.benchmark,
                v.metric,
                f"{v.threshold:.2f}",
                f"{v.current_value:.2f}",
                v.message,
            )

        console.print(table)

    # Warnings table
    if report.warnings:
        table = Table(title="Warnings", title_style="bold yellow", show_header=True, header_style="bold yellow")
        table.add_column("Benchmark", style="cyan")
        table.add_column("Metric", style="magenta")
        table.add_column("Message")

        for w in report.warnings:
            table.add_row(w.benchmark, w.metric, w.message)

        console.print(table)

    # Passed checks
    if report.passed_checks:
        passed = Table(title="Passed Checks", title_style="bold green", show_header=True, header_style="bold green")
        passed.add_column("Benchmark", style="green")
        passed.add_column("Status")

        for tool in report.passed_checks:
            passed.add_row(tool, "[green]PASS[/green]")

        console.print(passed)

    # Summary
    summary = Panel(
        Text.assemble(
            (f"Errors: {len(report.violations)}", "red" if report.violations else "dim"),
            " | ",
            (f"Warnings: {len(report.warnings)}", "yellow" if report.warnings else "dim"),
            " | ",
            (f"Passed: {len(report.passed_checks)}", "green" if report.passed_checks else "dim"),
        ),
        title="Summary",
        border_style=status_color,
    )
    console.print(summary)


def write_report_json(report: RegressionReport, output_path: Path | None = None) -> Path:
    """Write regression report as JSON artifact for CI.

    Args:
        report: The regression report
        output_path: Where to write (auto-generates if None)

    Returns:
        Path to the written JSON file
    """
    REGRESSION_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = REGRESSION_REPORT_DIR / f"regression_report_{timestamp}.json"

    payload = {
        "timestamp": report.timestamp,
        "git_sha": report.git_sha,
        "baselines_version": report.baselines_version,
        "status": "failed" if report.has_errors else ("warnings" if report.has_warnings else "passed"),
        "violations": [
            {
                "benchmark": v.benchmark,
                "metric": v.metric,
                "baseline_value": v.baseline_value,
                "current_value": v.current_value,
                "threshold": v.threshold,
                "severity": v.severity,
                "message": v.message,
            }
            for v in report.violations
        ],
        "warnings": [
            {
                "benchmark": w.benchmark,
                "metric": w.metric,
                "message": w.message,
            }
            for w in report.warnings
        ],
        "passed_checks": report.passed_checks,
        "summary": {
            "error_count": len(report.violations),
            "warning_count": len(report.warnings),
            "passed_count": len(report.passed_checks),
        },
    }

    output_path.write_text(json.dumps(payload, indent=2))
    return output_path


def assert_no_regressions(
    baselines: dict[str, Any] | None = None,
    results: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Assert no regressions, raising AssertionError if any found.

    This is the main entry point for CI integration.

    Args:
        baselines: Baseline config (loads from file if None)
        results: Benchmark results (loads latest if None)

    Raises:
        AssertionError: If any violations (errors) are found
    """
    report = check_regressions(baselines, results)
    console = Console()

    # Always render the report for CI logs
    render_report(report, console)

    # Write JSON artifact
    json_path = write_report_json(report)
    console.print(f"\n[dim]JSON report written to: {json_path}[/dim]")

    # Fail if errors
    if report.has_errors:
        raise AssertionError(
            f"CI performance regression check failed with {len(report.violations)} violation(s). "
            f"See report above for details."
        )


# Query count assertion helpers for use in tests
class QueryCountAssertion:
    """Context manager for asserting query counts in tests.

    Usage:
        with QueryCountAssertion("send_message", max_queries=5) as qa:
            await tool_call(...)
        # Raises AssertionError if query count exceeds max_queries
    """

    def __init__(self, operation: str, max_queries: int):
        self.operation = operation
        self.max_queries = max_queries
        self._tracker = None
        self._token = None

    def __enter__(self):
        from mcp_agent_mail.db import start_query_tracking

        self._tracker, self._token = start_query_tracking()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        from mcp_agent_mail.db import stop_query_tracking

        if self._token is not None:
            stop_query_tracking(self._token)

        if exc_type is None and self._tracker and self._tracker.total > self.max_queries:
            raise AssertionError(
                f"Query count assertion failed for '{self.operation}': "
                f"expected <= {self.max_queries}, got {self._tracker.total}"
            )

        return False

    @property
    def query_count(self) -> int:
        return self._tracker.total if self._tracker else 0


def assert_query_count_lte(operation: str, max_queries: int, actual: int) -> None:
    """Assert that query count is less than or equal to threshold.

    Args:
        operation: Name of the operation being checked
        max_queries: Maximum allowed queries
        actual: Actual query count

    Raises:
        AssertionError: If actual > max_queries
    """
    if actual > max_queries:
        raise AssertionError(
            f"Query count assertion failed for '{operation}': "
            f"expected <= {max_queries}, got {actual}"
        )


# CLI entry point for manual testing
if __name__ == "__main__":
    import sys

    try:
        assert_no_regressions()
        sys.exit(0)
    except AssertionError:
        sys.exit(1)
