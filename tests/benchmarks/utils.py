"""Shared helpers for benchmark tests with rich logging and JSON summaries."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import track_queries

DEFAULT_SEED = 1337
RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass(slots=True)
class BenchmarkSample:
    latency_ms: float
    query_count: int
    query_time_ms: float


@dataclass(slots=True)
class BenchmarkResult:
    name: str
    tool: str
    iterations: int
    dataset: dict[str, Any]
    seed: int
    latencies_ms: list[float]
    query_counts: list[int]
    query_time_ms: list[float]
    total_time_ms: float
    memory_peak_bytes: int
    errors: int = 0


@dataclass
class BenchHarness:
    mcp: Any
    project_key: str
    agent_name: str
    call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]]
    read_resource: Optional[Callable[[str], Awaitable[Any]]] = None


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def _latency_stats(latencies_ms: list[float]) -> dict[str, float]:
    if not latencies_ms:
        return {}
    return {
        "min": min(latencies_ms),
        "max": max(latencies_ms),
        "mean": statistics.mean(latencies_ms),
        "p50": percentile(latencies_ms, 50),
        "p95": percentile(latencies_ms, 95),
        "p99": percentile(latencies_ms, 99),
    }


def _query_stats(query_counts: list[int], query_times_ms: list[float]) -> dict[str, float]:
    if not query_counts:
        return {}
    total_queries = sum(query_counts)
    total_query_time = sum(query_times_ms)
    iterations = len(query_counts)
    return {
        "total": total_queries,
        "avg_per_op": total_queries / iterations,
        "total_time_ms": total_query_time,
        "avg_time_ms": total_query_time / iterations,
    }


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _get_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_message_body(seed: int, index: int, size: int) -> str:
    base = f"bench-{seed}-{index}-" * 64
    if size <= len(base):
        return base[:size]
    return (base * ((size // len(base)) + 1))[:size]


def _render_summary(console: Console, result: BenchmarkResult) -> None:
    latency = _latency_stats(result.latencies_ms)
    queries = _query_stats(result.query_counts, result.query_time_ms)

    header = Panel.fit(
        f"[bold cyan]Benchmark[/bold cyan]: [white]{result.name}[/white]"
        f"\n[dim]Tool[/dim]: {result.tool} | [dim]Iterations[/dim]: {result.iterations}",
        border_style="cyan",
    )
    console.print(header)

    dataset_table = Table(title="Dataset", show_header=True, header_style="bold magenta")
    dataset_table.add_column("Key", style="dim")
    dataset_table.add_column("Value")
    dataset_table.add_row("seed", str(result.seed))
    for key, value in result.dataset.items():
        dataset_table.add_row(str(key), str(value))
    console.print(dataset_table)

    metrics = Table(title="Latency & Throughput", show_header=True, header_style="bold green")
    metrics.add_column("Metric", style="dim")
    metrics.add_column("Value")
    for key in ("min", "mean", "p50", "p95", "p99", "max"):
        if key in latency:
            metrics.add_row(f"{key} (ms)", f"{latency[key]:.2f}")
    metrics.add_row("total time (ms)", f"{result.total_time_ms:.2f}")
    throughput = (result.iterations / (result.total_time_ms / 1000.0)) if result.total_time_ms > 0 else 0.0
    metrics.add_row("throughput (ops/sec)", f"{throughput:.2f}")
    console.print(metrics)

    query_table = Table(title="Query Counts", show_header=True, header_style="bold yellow")
    query_table.add_column("Metric", style="dim")
    query_table.add_column("Value")
    if queries:
        query_table.add_row("total queries", f"{queries['total']:.0f}")
        query_table.add_row("avg queries/op", f"{queries['avg_per_op']:.2f}")
        query_table.add_row("total query time (ms)", f"{queries['total_time_ms']:.2f}")
        query_table.add_row("avg query time/op (ms)", f"{queries['avg_time_ms']:.2f}")
    else:
        query_table.add_row("total queries", "0")
    console.print(query_table)

    memory_table = Table(title="Memory", show_header=True, header_style="bold blue")
    memory_table.add_column("Metric", style="dim")
    memory_table.add_column("Value")
    memory_table.add_row("peak", _format_bytes(result.memory_peak_bytes))
    console.print(memory_table)


def _json_payload(result: BenchmarkResult) -> dict[str, Any]:
    settings = get_settings()
    latency = _latency_stats(result.latencies_ms)
    queries = _query_stats(result.query_counts, result.query_time_ms)
    throughput = (result.iterations / (result.total_time_ms / 1000.0)) if result.total_time_ms > 0 else 0.0
    return {
        "schema_version": 1,
        "benchmark": result.name,
        "tool": result.tool,
        "timestamp": _now_iso(),
        "git_sha": _get_git_sha(),
        "seed": result.seed,
        "iterations": result.iterations,
        "dataset": result.dataset,
        "latency_ms": latency,
        "throughput_ops_sec": throughput,
        "query_stats": queries,
        "memory_peak_bytes": result.memory_peak_bytes,
        "instrumentation": {
            "enabled": settings.instrumentation_enabled,
            "slow_query_ms": settings.instrumentation_slow_query_ms,
        },
        "errors": result.errors,
    }


def _write_json(result: BenchmarkResult) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = result.name.replace(" ", "_").replace("/", "-")
    filename = f"{safe_name}_{result.seed}_{int(time.time())}.json"
    path = RESULTS_DIR / filename
    payload = _json_payload(result)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def _log_json_path(console: Console, path: Path) -> None:
    console.print(Panel.fit(f"[bold green]JSON summary written[/bold green]: {path}"))


def _should_run_benchmarks() -> bool:
    if os.getenv("RUN_BENCHMARKS") == "1":
        return True
    if os.getenv("BENCHMARKS") == "1":
        return True
    return os.getenv("CI") != "true"


def benchmark_enabled_reason() -> str:
    if _should_run_benchmarks():
        return "Benchmarks enabled."
    return "Benchmarks disabled by default. Set RUN_BENCHMARKS=1 to run."


async def run_benchmark(
    name: str,
    tool: str,
    iterations: int,
    seed: int,
    dataset: dict[str, Any],
    operation: Callable[[int], Awaitable[None]],
    *,
    warmup: int = 2,
) -> BenchmarkResult:
    console = Console()
    console.print(Panel.fit(f"[bold]Starting benchmark[/bold]: {name}", border_style="bright_blue"))

    if warmup > 0:
        console.print(f"[dim]Warmup iterations:[/dim] {warmup}")
        for i in range(warmup):
            await operation(-1 - i)

    tracemalloc.start()
    start_total = time.perf_counter()
    latencies: list[float] = []
    query_counts: list[int] = []
    query_times: list[float] = []

    for i in range(iterations):
        with track_queries() as tracker:
            start = time.perf_counter()
            await operation(i)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
        latencies.append(elapsed_ms)
        query_counts.append(tracker.total)
        query_times.append(tracker.total_time_ms)

    total_time_ms = (time.perf_counter() - start_total) * 1000.0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result = BenchmarkResult(
        name=name,
        tool=tool,
        iterations=iterations,
        dataset=dataset,
        seed=seed,
        latencies_ms=latencies,
        query_counts=query_counts,
        query_time_ms=query_times,
        total_time_ms=total_time_ms,
        memory_peak_bytes=peak,
    )

    _render_summary(console, result)
    json_path = _write_json(result)
    _log_json_path(console, json_path)
    return result
