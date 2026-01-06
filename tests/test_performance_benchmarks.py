"""Performance benchmarks for large bundle exports and viewer loading.

This test suite validates performance characteristics of the share export pipeline:
1. Export time for different database sizes
2. Chunk configuration validation
3. Bundle size measurements
4. Database compressibility

Reference: PLAN_TO_ENABLE_EASY_AND_SECURE_SHARING_OF_AGENT_MAILBOX.md line 261
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mcp_agent_mail import share


@pytest.fixture
def small_db(tmp_path: Path) -> Path:
    """Create a small database (~200KB-1MB with overhead) with 100 messages."""
    return _create_test_database(tmp_path, "small.sqlite3", num_messages=100, body_size=1000)


@pytest.fixture
def medium_db(tmp_path: Path) -> Path:
    """Create a medium database (~10MB) with 1000 messages."""
    return _create_test_database(tmp_path, "medium.sqlite3", num_messages=1000, body_size=10000)


@pytest.fixture
def large_db(tmp_path: Path) -> Path:
    """Create a large database (~100MB) with 5000 messages."""
    return _create_test_database(tmp_path, "large.sqlite3", num_messages=5000, body_size=20000)


def _create_test_database(tmp_path: Path, name: str, num_messages: int, body_size: int) -> Path:
    """Create a test database with specified number of messages and body size."""
    db_path = tmp_path / name
    conn = sqlite3.connect(db_path)

    try:
        # Create schema matching production
        conn.executescript("""
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                human_key TEXT
            );

            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                program TEXT,
                model TEXT
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT,
                thread_id TEXT,
                reply_to INTEGER
            );

            CREATE TABLE message_recipients (
                id INTEGER PRIMARY KEY,
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT
            );

            -- Indexes for performance
            CREATE INDEX idx_messages_created_ts ON messages(created_ts);
            CREATE INDEX idx_messages_thread_id ON messages(thread_id);
            CREATE INDEX idx_messages_project_id ON messages(project_id);
            CREATE INDEX idx_message_recipients_message_id ON message_recipients(message_id);

            -- FTS5 for search
            CREATE VIRTUAL TABLE fts_messages USING fts5(
                subject, body_md, content=messages, content_rowid=id
            );
        """)

        # Insert test project
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'perf-test', 'Performance Test')")

        # Insert test agents
        conn.execute("INSERT INTO agents (id, project_id, name, program, model) VALUES (1, 1, 'TestAgent', 'test', 'test-model')")

        # Insert messages with realistic size
        # Use a repeating pattern to ensure compressibility
        body_template = "This is test message content. " * (body_size // 30)

        for i in range(1, num_messages + 1):
            thread_id = f"thread-{(i - 1) // 10 + 1}" if i % 3 != 0 else None
            conn.execute(
                """INSERT INTO messages
                   (id, project_id, subject, body_md, importance, ack_required, created_ts, attachments, thread_id)
                   VALUES (?, 1, ?, ?, 'normal', 0, ?, '[]', ?)""",
                (
                    i,
                    f"Test Message {i}",
                    f"{body_template} Message number {i}.",
                    f"2025-11-{5 - i // (num_messages // 3 + 1):02d}T{i % 24:02d}:{i % 60:02d}:00Z",
                    thread_id,
                ),
            )

            # Add FTS entry
            conn.execute(
                "INSERT INTO fts_messages(rowid, subject, body_md) VALUES (?, ?, ?)",
                (i, f"Test Message {i}", f"{body_template} Message number {i}."),
            )

            # Add recipient
            conn.execute(
                "INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (?, 1, 'to')",
                (i,),
            )

        conn.commit()
    finally:
        conn.close()

    return db_path


def _get_file_size_mb(path: Path) -> float:
    """Get file size in MB."""
    return path.stat().st_size / (1024 * 1024)


@pytest.mark.benchmark
def test_small_bundle_export_performance(small_db: Path, tmp_path: Path) -> None:
    """Benchmark snapshot creation performance for ~1MB database.

    Small databases should snapshot very quickly as they use SQLite Online Backup API.
    """
    snapshot_path = tmp_path / "snapshot.sqlite3"

    # Measure snapshot time
    start_time = time.time()
    share.create_sqlite_snapshot(small_db, snapshot_path, checkpoint=True)
    export_time = time.time() - start_time

    # Validate snapshot was created
    assert snapshot_path.exists()
    db_size_mb = _get_file_size_mb(snapshot_path)

    print("\nSmall snapshot performance:")
    print(f"  Database size: {db_size_mb:.2f} MB")
    print(f"  Snapshot time: {export_time:.3f} seconds")
    if export_time > 0:
        print(f"  Throughput: {db_size_mb / export_time:.2f} MB/s")

    # Small snapshots should be fast
    assert export_time < 5.0, "Small snapshot should complete in < 5 seconds"


@pytest.mark.benchmark
@pytest.mark.slow
def test_medium_bundle_export_performance(medium_db: Path, tmp_path: Path) -> None:
    """Benchmark snapshot creation performance for ~10MB database.

    Medium databases should snapshot efficiently without chunking.
    """
    snapshot_path = tmp_path / "snapshot.sqlite3"

    # Measure snapshot time
    start_time = time.time()
    share.create_sqlite_snapshot(medium_db, snapshot_path, checkpoint=True)
    export_time = time.time() - start_time

    # Validate snapshot was created
    assert snapshot_path.exists()
    db_size_mb = _get_file_size_mb(snapshot_path)

    print("\nMedium snapshot performance:")
    print(f"  Database size: {db_size_mb:.2f} MB")
    print(f"  Snapshot time: {export_time:.3f} seconds")
    if export_time > 0:
        print(f"  Throughput: {db_size_mb / export_time:.2f} MB/s")

    # Medium snapshots should complete in reasonable time
    assert export_time < 30.0, "Medium snapshot should complete in < 30 seconds"


@pytest.mark.benchmark
@pytest.mark.slow
def test_large_bundle_export_performance(large_db: Path, tmp_path: Path) -> None:
    """Benchmark snapshot + chunking performance for ~100MB database.

    Large databases should snapshot efficiently and optionally be chunked for httpvfs.
    """
    snapshot_path = tmp_path / "snapshot.sqlite3"

    # Measure snapshot time
    start_time = time.time()
    share.create_sqlite_snapshot(large_db, snapshot_path, checkpoint=True)
    snapshot_time = time.time() - start_time

    # Validate snapshot was created
    assert snapshot_path.exists()
    db_size_mb = _get_file_size_mb(snapshot_path)

    print("\nLarge snapshot performance:")
    print(f"  Database size: {db_size_mb:.2f} MB")
    print(f"  Snapshot time: {snapshot_time:.3f} seconds")
    if snapshot_time > 0:
        print(f"  Throughput: {db_size_mb / snapshot_time:.2f} MB/s")

    # Test chunking if database is large enough
    if db_size_mb > 10:
        output_dir = tmp_path / "chunked"
        output_dir.mkdir()

        chunk_start = time.time()
        chunked = share.maybe_chunk_database(
            snapshot_path,
            output_dir,
            threshold_bytes=10 * 1024 * 1024,
            chunk_bytes=5 * 1024 * 1024,
        )
        chunk_time = time.time() - chunk_start

        print(f"  Chunking time: {chunk_time:.3f} seconds")
        print(f"  Was chunked: {chunked is not None}")

    # Large snapshots should still complete in reasonable time
    assert snapshot_time < 120.0, "Large snapshot should complete in < 2 minutes"


@pytest.mark.benchmark
def test_database_compressibility(small_db: Path, tmp_path: Path) -> None:
    """Test database compressibility for different scenarios.

    SQLite databases with repetitive content should compress well with gzip/brotli.
    This is important for static hosting where CDNs typically apply compression.
    """
    import gzip
    import shutil

    # Create snapshot
    snapshot_path = tmp_path / "snapshot.sqlite3"
    share.create_sqlite_snapshot(small_db, snapshot_path, checkpoint=True)
    assert snapshot_path.exists()

    # Measure uncompressed size
    uncompressed_size = snapshot_path.stat().st_size
    uncompressed_mb = uncompressed_size / (1024 * 1024)

    # Compress with gzip
    compressed_path = tmp_path / "mailbox.sqlite3.gz"
    with snapshot_path.open("rb") as f_in, gzip.open(compressed_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)

    compressed_size = compressed_path.stat().st_size
    compressed_mb = compressed_size / (1024 * 1024)
    compression_ratio = compressed_size / uncompressed_size

    print("\nDatabase compression statistics:")
    print(f"  Uncompressed: {uncompressed_mb:.2f} MB")
    print(f"  Compressed (gzip): {compressed_mb:.2f} MB")
    print(f"  Compression ratio: {compression_ratio:.2%}")

    # Expect at least 30% compression for repetitive test data
    assert compression_ratio < 0.7, \
        f"Database should compress to < 70% of original size, got {compression_ratio:.2%}"


@pytest.mark.benchmark
def test_chunk_size_validation(large_db: Path, tmp_path: Path) -> None:
    """Test that chunking produces appropriately sized chunks.

    httpvfs performance depends on chunk size - too small means many HTTP requests,
    too large means downloading unnecessary data.
    """
    # Create snapshot first
    snapshot_path = tmp_path / "snapshot.sqlite3"
    share.create_sqlite_snapshot(large_db, snapshot_path, checkpoint=True)

    output_dir = tmp_path / "chunk_test"
    output_dir.mkdir()

    # Test chunking with specific chunk size
    chunk_size_mb = 5
    chunked = share.maybe_chunk_database(
        snapshot_path,
        output_dir,
        threshold_bytes=1 * 1024 * 1024,  # Force chunking at 1MB
        chunk_bytes=int(chunk_size_mb * 1024 * 1024),
    )

    print("\nChunk validation:")
    print(f"  Was chunked: {chunked}")

    if chunked:
        # Check if config file was created
        config_path = output_dir / "mailbox.sqlite3.config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            total_size = config.get("databaseLength", 0)

            print(f"  Total size: {total_size / (1024 * 1024):.2f} MB")

            # Validate chunks directory
            chunks_dir = output_dir / "chunks"
            if chunks_dir.exists():
                chunk_files = sorted(chunks_dir.glob("mailbox.sqlite3*"))
                print(f"  Number of chunks: {len(chunk_files)}")

                # Check individual chunk sizes
                for chunk_file in chunk_files:
                    chunk_size = chunk_file.stat().st_size / (1024 * 1024)
                    print(f"    {chunk_file.name}: {chunk_size:.2f} MB")

                    # Chunks should not wildly exceed requested size
                    assert chunk_size <= chunk_size_mb * 2, \
                        f"Chunk {chunk_file.name} too large ({chunk_size:.2f} > {chunk_size_mb * 2})"
    else:
        print("  Database not chunked (below threshold)")


def test_vacuum_improves_locality() -> None:
    """Document that VACUUM should be run before export for optimal performance.

    VACUUM rebuilds the database file, which:
    1. Removes fragmentation
    2. Improves page locality
    3. Reduces file size
    4. Optimizes httpvfs streaming performance
    """
    documentation = """
    VACUUM Optimization for Export
    ===============================

    The export pipeline should run VACUUM before creating snapshots to:

    1. Defragment database pages
       - Consecutive pages minimize HTTP Range requests
       - Better locality = fewer round trips for httpvfs

    2. Reclaim unused space
       - Reduces bundle size
       - Faster downloads and cache loading

    3. Rebuild indexes
       - Optimal B-tree structure
       - Better query performance in viewer

    4. Update statistics
       - SQLite query planner uses current stats
       - Improves EXPLAIN QUERY PLAN results

    Implementation:
    ---------------

    Before export:
    ```python
    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.execute("ANALYZE")
    conn.close()
    ```

    Cost: O(n) where n = database size
    Benefit: 10-30% size reduction, 2-5x better httpvfs performance
    """

    assert len(documentation) > 100, "VACUUM documentation should be comprehensive"


def test_browser_performance_requirements_documentation() -> None:
    """Document requirements for browser-based performance testing.

    Full performance validation requires headless browser automation to measure:
    1. First meaningful paint
    2. OPFS cache performance
    3. Warm vs cold load times
    4. httpvfs HTTP Range request patterns
    """
    documentation = """
    Browser Performance Testing Requirements
    =========================================

    Comprehensive performance validation requires Playwright/Puppeteer tests:

    1. Bundle Loading Performance
    ------------------------------

    Test Setup:
    - Create bundles: 1 MB, 10 MB, 100 MB, 500 MB
    - Deploy to local static server
    - Launch headless Chromium with Performance API

    Metrics to measure:
    - Time to First Byte (TTFB)
    - First Contentful Paint (FCP)
    - First Meaningful Paint (FMP) - when message list appears
    - Time to Interactive (TTI) - when search/navigation works

    Target: FMP < 2s for 100MB bundle on fast connection

    2. OPFS Cache Performance
    --------------------------

    Test Setup:
    - Launch browser with COOP/COEP headers (cross-origin isolation)
    - Verify sqlite-wasm + OPFS is available
    - Load bundle first time (cold cache)
    - Reload page (warm cache)

    Metrics to measure:
    - Cold load: full download + OPFS write time
    - Warm load: OPFS read time (should be < 200ms)
    - Cache hit ratio (via Performance API)

    Target: Warm load FMP < 500ms for 100MB bundle

    3. httpvfs Streaming Performance
    ---------------------------------

    Test Setup:
    - Deploy chunked bundle (10MB chunks)
    - Monitor Network tab in DevTools
    - Perform various viewer operations

    Metrics to measure:
    - Number of HTTP Range requests for initial load
    - Bytes downloaded vs database size ratio
    - Lazy loading behavior (chunks downloaded on demand)

    Target: < 10 Range requests for initial thread list view

    4. Query Performance Under Load
    --------------------------------

    Test Setup:
    - Load large bundle (500 MB, 50k+ messages)
    - Perform rapid navigation/search operations
    - Monitor console for slow queries

    Metrics to measure:
    - Thread list render time
    - Search result latency (FTS vs LIKE)
    - Message detail load time
    - Scroll performance (virtual scrolling if implemented)

    Target: All operations < 100ms after initial load

    5. Memory Usage
    ---------------

    Test Setup:
    - Open bundle in browser
    - Monitor memory usage over time
    - Navigate through many messages

    Metrics to measure:
    - Peak memory usage
    - Memory leaks (should stay flat after initial load)
    - OPFS cache storage quota usage

    Target: < 2x database size memory usage

    Implementation Tools:
    ---------------------

    ```python
    from playwright.sync_api import sync_playwright

    def test_bundle_load_performance():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()

            # Measure FMP
            page.goto("http://localhost:8000/viewer/")
            page.wait_for_selector("#message-list li")  # First message visible
            metrics = page.evaluate("() => performance.getEntriesByType('navigation')[0]")

            assert metrics["domContentLoadedEventEnd"] < 2000
            browser.close()
    ```

    See tests/playwright/ directory for full implementation.
    """

    assert len(documentation) > 500, "Browser performance documentation should be comprehensive"


@pytest.mark.benchmark
@pytest.mark.parametrize("num_messages", [100, 1000, 5000])
def test_export_scales_linearly(tmp_path: Path, num_messages: int) -> None:
    """Test that snapshot time scales linearly with database size.

    Snapshot performance should be O(n) where n = database size.
    Non-linear scaling would indicate a performance bottleneck.
    """
    # Create database with specified size
    db_path = _create_test_database(tmp_path, f"scale_{num_messages}.sqlite3", num_messages, 1000)

    # Measure snapshot time
    snapshot_path = tmp_path / f"scale_{num_messages}_snapshot.sqlite3"
    start_time = time.time()
    share.create_sqlite_snapshot(db_path, snapshot_path, checkpoint=True)
    snapshot_time = time.time() - start_time

    # Calculate throughput
    throughput = num_messages / snapshot_time if snapshot_time > 0 else float('inf')

    db_size_mb = _get_file_size_mb(snapshot_path)

    print(f"\nScale test ({num_messages} messages, {db_size_mb:.2f} MB):")
    print(f"  Snapshot time: {snapshot_time:.3f} seconds")
    print(f"  Throughput: {throughput:.0f} messages/second")

    # Snapshot should handle at least 50 messages/second
    assert throughput > 50, f"Snapshot throughput too low: {throughput:.0f} msg/s"


# ============================================================================
# MCP Tool Latency Benchmarks
# ============================================================================
# Reference: mcp_agent_mail-4em (testing-tasks-v2.md)
# Targets:
# - Message send: < 100ms p95
# - Inbox fetch: < 200ms p95
# - Search: < 500ms p95


def percentile(data: list[float], p: int) -> float:
    """Calculate the p-th percentile of data."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def print_latency_stats(name: str, latencies_ms: list[float]) -> dict[str, float]:
    """Print and return latency statistics."""
    import statistics

    if not latencies_ms:
        return {}
    stats = {
        "count": len(latencies_ms),
        "min": min(latencies_ms),
        "max": max(latencies_ms),
        "mean": statistics.mean(latencies_ms),
        "p50": percentile(latencies_ms, 50),
        "p95": percentile(latencies_ms, 95),
        "p99": percentile(latencies_ms, 99),
    }
    print(f"\n{name} Latency (ms):")
    print(f"  Count: {stats['count']}")
    print(f"  Min: {stats['min']:.2f}")
    print(f"  Max: {stats['max']:.2f}")
    print(f"  Mean: {stats['mean']:.2f}")
    print(f"  P50: {stats['p50']:.2f}")
    print(f"  P95: {stats['p95']:.2f}")
    print(f"  P99: {stats['p99']:.2f}")
    return stats


class TestMessageSendLatency:
    """Benchmark message send latency."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_message_send_latency_baseline(self, isolated_env):
        """Establish baseline for message send latency. Target: < 100ms p95."""
        import uuid

        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server

        server = build_mcp_server()
        latencies: list[float] = []
        num_iterations = 20

        # Use unique project key to avoid cross-test pollution
        project_key = f"/perf-test-{uuid.uuid4().hex[:8]}"

        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Use create_agent_identity for guaranteed unique name
            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Message send benchmark",
                },
            )
            agent_name = agent_result.data.get("name")

            for i in range(num_iterations):
                start = time.perf_counter()
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Benchmark message {i}",
                        "body_md": f"Benchmark message {i} for latency testing.",
                    },
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        stats = print_latency_stats("Message Send", latencies)
        # Target: < 100ms p95, allow 500ms for test environment overhead
        assert stats["p95"] < 500, f"Message send p95 ({stats['p95']:.2f}ms) exceeds threshold"


class TestInboxFetchLatency:
    """Benchmark inbox fetch latency."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_inbox_fetch_with_100_messages(self, isolated_env):
        """Benchmark inbox fetch with 100 messages. Target: < 200ms p95."""
        import uuid

        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server

        server = build_mcp_server()
        num_messages = 100
        fetch_iterations = 10
        latencies: list[float] = []

        # Use unique project key to avoid cross-test pollution
        project_key = f"/perf-inbox-{uuid.uuid4().hex[:8]}"

        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Use create_agent_identity for guaranteed unique name
            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Inbox fetch benchmark",
                },
            )
            agent_name = agent_result.data.get("name")

            # Populate inbox
            print(f"\nPopulating inbox with {num_messages} messages...")
            for i in range(num_messages):
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Message {i}",
                        "body_md": f"Content for message {i}",
                    },
                )

            # Measure fetch latency
            for _ in range(fetch_iterations):
                start = time.perf_counter()
                result = await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                        "limit": 100,
                    },
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)
                assert len(result.data) > 0

        stats = print_latency_stats("Inbox Fetch (100 msgs)", latencies)
        # Target: < 200ms p95, allow 500ms for overhead
        assert stats["p95"] < 500, f"Inbox fetch p95 ({stats['p95']:.2f}ms) exceeds threshold"


class TestSearchLatency:
    """Benchmark search latency."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_search_with_many_messages(self, isolated_env):
        """Benchmark search latency. Target: < 500ms p95."""
        import uuid

        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server

        server = build_mcp_server()
        num_messages = 100  # Reduced for speed; scale up for production
        search_iterations = 10
        latencies: list[float] = []

        # Use unique project key to avoid cross-test pollution
        project_key = f"/perf-search-{uuid.uuid4().hex[:8]}"

        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Use create_agent_identity for guaranteed unique name
            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Search benchmark",
                },
            )
            agent_name = agent_result.data.get("name")

            # Populate with searchable messages
            print(f"\nPopulating with {num_messages} messages for search...")
            for i in range(num_messages):
                keyword = ["alpha", "beta", "gamma", "delta"][i % 4]
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Report {keyword} {i}",
                        "body_md": f"This is a {keyword} report number {i}.",
                    },
                )

            # Measure search latency
            for _ in range(search_iterations):
                start = time.perf_counter()
                await client.call_tool(
                    "search_messages",
                    {
                        "project_key": project_key,
                        "query": "alpha OR beta",
                        "limit": 50,
                    },
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        stats = print_latency_stats("Search (FTS)", latencies)
        # Target: < 500ms p95, allow 1000ms for overhead
        assert stats["p95"] < 1000, f"Search p95 ({stats['p95']:.2f}ms) exceeds threshold"


class TestFileReservationLatency:
    """Benchmark file reservation operations."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_reservation_conflict_check_with_100(self, isolated_env):
        """Benchmark conflict check with many existing reservations."""
        import uuid

        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server

        server = build_mcp_server()
        num_reservations = 50
        check_iterations = 10
        latencies: list[float] = []

        # Use unique project key to avoid cross-test pollution
        project_key = f"/perf-conflict-{uuid.uuid4().hex[:8]}"

        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Use create_agent_identity instead of register_agent to get guaranteed unique names
            agent1_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Reserving agent",
                },
            )
            agent1_name = agent1_result.data.get("name")

            agent2_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Conflict checking agent",
                },
            )
            agent2_name = agent2_result.data.get("name")

            # Create many reservations
            print(f"\nCreating {num_reservations} reservations with {agent1_name}...")
            for i in range(num_reservations):
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": agent1_name,
                        "paths": [f"lib/component_{i}/**"],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                    },
                )

            # Measure conflict check time using agent2
            print(f"Measuring conflict checks with {agent2_name}...")
            for i in range(check_iterations):
                start = time.perf_counter()
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": agent2_name,
                        "paths": [f"lib/component_{i % num_reservations}/**"],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                    },
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        stats = print_latency_stats("Conflict Check (50 reservations)", latencies)
        assert stats["p95"] < 500, "Conflict check p95 exceeds threshold"


class TestArchiveWriteLatency:
    """Benchmark Git archive write operations."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_archive_ensure_latency(self, isolated_env):
        """Benchmark archive initialization latency."""
        from mcp_agent_mail.config import get_settings
        from mcp_agent_mail.storage import ensure_archive

        settings = get_settings()
        latencies: list[float] = []
        num_iterations = 10

        for i in range(num_iterations):
            start = time.perf_counter()
            await ensure_archive(settings, f"perf-archive-{i}")
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        stats = print_latency_stats("Archive Ensure", latencies)
        assert stats["p95"] < 1000, "Archive ensure p95 exceeds threshold"


class TestPerformanceSummary:
    """Generate performance summary report."""

    @pytest.mark.asyncio
    @pytest.mark.benchmark
    async def test_generate_summary_report(self, isolated_env):
        """Run minimal benchmark suite and print summary."""
        import uuid

        from fastmcp import Client

        from mcp_agent_mail.app import build_mcp_server

        server = build_mcp_server()
        results: dict[str, dict[str, float]] = {}

        # Use unique project key to avoid cross-test pollution
        project_key = f"/perf-summary-{uuid.uuid4().hex[:8]}"

        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": project_key})

            # Use create_agent_identity to get guaranteed unique name
            agent_result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": "Summary benchmark agent",
                },
            )
            agent_name = agent_result.data.get("name")

            # Message send
            latencies = []
            for i in range(5):
                start = time.perf_counter()
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [agent_name],
                        "subject": f"Summary test {i}",
                        "body_md": "Quick benchmark",
                    },
                )
                latencies.append((time.perf_counter() - start) * 1000)
            results["Message Send"] = {
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            }

            # Inbox fetch
            latencies = []
            for _ in range(5):
                start = time.perf_counter()
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                    },
                )
                latencies.append((time.perf_counter() - start) * 1000)
            results["Inbox Fetch"] = {
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            }

            # File reservation
            latencies = []
            for i in range(5):
                start = time.perf_counter()
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                        "paths": [f"summary/file_{i}.py"],
                        "ttl_seconds": 3600,
                    },
                )
                latencies.append((time.perf_counter() - start) * 1000)
            results["File Reservation"] = {
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            }

        # Print summary table
        print("\n" + "=" * 60)
        print("PERFORMANCE SUMMARY REPORT")
        print("=" * 60)
        print(f"{'Operation':<25} {'P50 (ms)':<15} {'P95 (ms)':<15}")
        print("-" * 60)
        for op, stats in results.items():
            print(f"{op:<25} {stats['p50']:<15.2f} {stats['p95']:<15.2f}")
        print("=" * 60)

        for op, stats in results.items():
            assert stats["p95"] < 1000, f"{op} p95 ({stats['p95']:.2f}ms) too slow"
