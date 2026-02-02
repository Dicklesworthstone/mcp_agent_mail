"""Pattern matching performance benchmarks for file reservation workflows.

Baselines (documented targets, not hard CI thresholds):
- PathSpec compile (single pattern): ~0.004ms, threshold <0.01ms
- PathSpec match (single pattern): ~0.0007ms, threshold <0.002ms
- Conflict detection (50 paths x 100 reservations): ~2.5ms, threshold <5ms
- Union PathSpec (100 patterns, 100 paths): ~0.3ms, threshold <1ms
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pathspec import PathSpec

from mcp_agent_mail.app import (
    _compile_pathspec,
    _file_reservations_conflict,
    _normalize_pathspec_pattern,
    _patterns_overlap,
)
from mcp_agent_mail.models import Agent, FileReservation

from .utils import DEFAULT_SEED, run_benchmark


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_reservations(count: int, *, project_id: int = 1, agent_id_start: int = 10) -> list[FileReservation]:
    now = _naive_utc_now()
    expires_ts = now + timedelta(hours=1)
    reservations: list[FileReservation] = []
    for i in range(count):
        reservations.append(
            FileReservation(
                id=i + 1,
                project_id=project_id,
                agent_id=agent_id_start + i,
                path_pattern=f"src/module{i % 50}/**/*.py",
                exclusive=True,
                reason="benchmark",
                created_ts=now,
                expires_ts=expires_ts,
            )
        )
    return reservations


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_pathspec_compile():
    seed = DEFAULT_SEED
    patterns = [
        "src/**/*.py",
        "tests/**",
        "docs/**/*.md",
        "assets/**/*.png",
        "scripts/*.sh",
        "deploy/**",
    ]

    async def operation(_i: int) -> None:
        for pattern in patterns:
            PathSpec.from_lines("gitignore", [pattern])

    await run_benchmark(
        name="pathspec_compile",
        tool="pathspec_compile",
        iterations=100,
        seed=seed,
        dataset={"pattern_count": len(patterns)},
        operation=operation,
        warmup=1,
    )


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_file_reservation_conflict_detection():
    seed = DEFAULT_SEED
    reservations = _build_reservations(100)
    paths = [f"src/module{i}/file{j}.py" for i in range(10) for j in range(5)]
    candidate_agent = Agent(
        id=999,
        project_id=1,
        name="BenchAgent",
        program="bench",
        model="bench",
        task_description="benchmark",
    )

    async def operation(_i: int) -> None:
        for path in paths:
            for reservation in reservations:
                _file_reservations_conflict(reservation, path, True, candidate_agent)

    await run_benchmark(
        name="file_reservation_conflict",
        tool="file_reservation_conflict",
        iterations=8,
        seed=seed,
        dataset={"path_count": len(paths), "reservation_count": len(reservations)},
        operation=operation,
        warmup=1,
    )


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_pathspec_cache_hits():
    seed = DEFAULT_SEED
    pattern = _normalize_pathspec_pattern("src/**/*.py")
    _compile_pathspec.cache_clear()
    _compile_pathspec(pattern)

    async def operation(_i: int) -> None:
        _compile_pathspec(pattern)

    await run_benchmark(
        name="pathspec_cache_hits",
        tool="pathspec_cache_hits",
        iterations=200,
        seed=seed,
        dataset={"pattern": pattern},
        operation=operation,
        warmup=0,
    )

    info = _compile_pathspec.cache_info()
    assert info.misses <= 1, f"expected cache miss <= 1, got {info.misses}"
    assert info.hits >= 200, f"expected at least 200 cache hits, got {info.hits}"


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_union_pathspec_matching():
    seed = DEFAULT_SEED
    patterns = [f"src/module{i}/**/*.py" for i in range(100)]
    paths = [f"src/module{i}/file{j}.py" for i in range(10) for j in range(10)]
    specs = [PathSpec.from_lines("gitignore", [pattern]) for pattern in patterns]
    union_spec = PathSpec.from_lines("gitignore", patterns)

    async def individual_operation(_i: int) -> None:
        for path in paths:
            for spec in specs:
                spec.match_file(path)

    async def union_operation(_i: int) -> None:
        set(union_spec.match_files(paths))

    individual_result = await run_benchmark(
        name="pathspec_match_individual",
        tool="pathspec_match_individual",
        iterations=5,
        seed=seed,
        dataset={"pattern_count": len(patterns), "path_count": len(paths)},
        operation=individual_operation,
        warmup=1,
    )

    union_result = await run_benchmark(
        name="pathspec_match_union",
        tool="pathspec_match_union",
        iterations=5,
        seed=seed,
        dataset={"pattern_count": len(patterns), "path_count": len(paths)},
        operation=union_operation,
        warmup=1,
    )

    speedup = (
        individual_result.total_time_ms / union_result.total_time_ms
        if union_result.total_time_ms > 0
        else 0.0
    )
    assert speedup >= 2.0, f"expected union PathSpec speedup >= 2x, got {speedup:.2f}x"


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_patterns_overlap():
    seed = DEFAULT_SEED
    pairs = [
        ("src/**", "src/module/file.py"),
        ("docs/**", "docs/readme.md"),
        ("assets/*.png", "assets/logo.png"),
        ("scripts/*.sh", "scripts/deploy.sh"),
        ("deploy/**", "deploy/config/prod.yaml"),
        ("tests/**/*.py", "tests/unit/test_app.py"),
    ]

    async def operation(_i: int) -> None:
        for left, right in pairs:
            _patterns_overlap(left, right)

    await run_benchmark(
        name="patterns_overlap",
        tool="patterns_overlap",
        iterations=200,
        seed=seed,
        dataset={"pair_count": len(pairs)},
        operation=operation,
        warmup=1,
    )
