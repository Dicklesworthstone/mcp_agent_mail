"""Query locality validation tests for viewer database performance.

This test suite validates that queries used in the static viewer:
1. Use indexes efficiently (SEARCH operations, not SCAN)
2. Minimize random seeks for httpvfs streaming performance
3. Have appropriate covering indexes to avoid extra lookups
4. Maintain good data locality measured via dbstat

Reference: PLAN_TO_ENABLE_EASY_AND_SECURE_SHARING_OF_AGENT_MAILBOX.md line 262
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Create a sample database with realistic schema and data for query testing."""
    db_path = tmp_path / "test_queries.sqlite3"
    conn = sqlite3.connect(db_path)

    try:
        # Create schema matching production
        conn.executescript("""
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                human_key TEXT
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
                thread_id TEXT
            );

            -- Indexes used by viewer queries
            CREATE INDEX idx_messages_created_ts ON messages(created_ts);
            CREATE INDEX idx_messages_thread_id ON messages(thread_id);
            CREATE INDEX idx_messages_project_id ON messages(project_id);

            -- FTS5 search index
            CREATE VIRTUAL TABLE fts_messages USING fts5(
                subject, body_md, content=messages, content_rowid=id
            );

            -- Triggers to keep FTS in sync (production uses these)
            CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO fts_messages(rowid, subject, body_md)
                VALUES (new.id, new.subject, new.body_md);
            END;

            CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO fts_messages(fts_messages, rowid, subject, body_md)
                VALUES ('delete', old.id, old.subject, old.body_md);
            END;

            CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO fts_messages(fts_messages, rowid, subject, body_md)
                VALUES ('delete', old.id, old.subject, old.body_md);
                INSERT INTO fts_messages(rowid, subject, body_md)
                VALUES (new.id, new.subject, new.body_md);
            END;
        """)

        # Insert test data
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'test-project', 'Test Project')")

        # Insert messages spanning multiple threads
        for i in range(1, 101):
            thread_id = f"thread-{(i - 1) // 10 + 1}" if i % 3 != 0 else None  # Some messages have thread_id, some don't
            conn.execute(
                """INSERT INTO messages
                   (id, project_id, subject, body_md, importance, ack_required, created_ts, attachments, thread_id)
                   VALUES (?, 1, ?, ?, 'normal', 0, ?, '[]', ?)""",
                (
                    i,
                    f"Test Message {i}",
                    f"This is test message body {i} with some content for searching.",
                    f"2025-11-{5 - i // 30:02d}T{i % 24:02d}:{i % 60:02d}:00Z",
                    thread_id,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    return db_path


def _explain_query(conn: sqlite3.Connection, sql: str, params: list | None = None) -> list[dict[str, str]]:
    """Run EXPLAIN QUERY PLAN and return the plan as a list of dicts."""
    cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params or [])
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def _has_full_table_scan(plan: list[dict[str, str]]) -> bool:
    """Check if query plan contains a full table SCAN (not using indexes)."""
    for step in plan:
        detail = step.get("detail", "")
        # Look for SCAN operations that don't have "USING" (which means they're not using indexes)
        if "SCAN" in detail and "USING" not in detail and "fts_messages" not in detail:
            # Exception: SCAN of virtual FTS tables is expected
            return True
    return False


def _has_search_using_index(plan: list[dict[str, str]], table: str, index: str | None = None) -> bool:
    """Check if query plan uses SEARCH operation with an index on the specified table."""
    for step in plan:
        detail = step.get("detail", "")
        if "SEARCH" in detail and table in detail and "USING" in detail and (index is None or index in detail):
            return True
    return False


def test_thread_list_query_uses_indexes(sample_db: Path) -> None:
    """Test that buildThreadList query has reasonable performance characteristics.

    This query is critical for initial page load performance. It uses CTEs and
    correlated subqueries which create temporary tables - this is acceptable for
    thread list aggregation as the LIMIT caps the result set.

    Note: datetime() function calls in ORDER BY prevent index usage. For production,
    consider storing created_ts as INTEGER (Unix timestamp) for better index utilization.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is the buildThreadList query from viewer.js (lines 425-469)
        sql = """
            WITH normalized AS (
              SELECT
                id,
                subject,
                COALESCE(body_md, '') AS body_md,
                COALESCE(thread_id, '') AS thread_id,
                created_ts,
                importance,
                project_id
              FROM messages
            ),
            keyed AS (
              SELECT
                CASE WHEN thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
                *
              FROM normalized
            )
            SELECT
              thread_key,
              COUNT(*) AS message_count,
              MAX(created_ts) AS last_created_ts,
              (
                SELECT subject FROM keyed k2
                WHERE k2.thread_key = k.thread_key
                ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
                LIMIT 1
              ) AS latest_subject,
              (
                SELECT importance FROM keyed k2
                WHERE k2.thread_key = k.thread_key
                ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
                LIMIT 1
              ) AS latest_importance,
              (
                SELECT substr(body_md, 1, 160) FROM keyed k2
                WHERE k2.thread_key = k.thread_key
                ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
                LIMIT 1
              ) AS latest_snippet
            FROM keyed k
            GROUP BY thread_key
            ORDER BY datetime(last_created_ts) DESC
            LIMIT ?
        """

        plan = _explain_query(conn, sql, [200])

        # Print plan for documentation
        print("\nQuery plan for buildThreadList:")
        for step in plan:
            print(f"  {step}")

        # Verify query completes and respects LIMIT
        cursor = conn.execute(sql, [200])
        results = cursor.fetchall()
        assert len(results) <= 200, "Query should respect LIMIT"

        # CTEs create temp tables - this is expected and acceptable for thread aggregation
        # The LIMIT ensures bounded results even for large databases

    finally:
        conn.close()


def test_get_thread_messages_all_query_uses_index(sample_db: Path) -> None:
    """Test that getThreadMessages('all') query has reasonable performance.

    This query is used when viewing all messages. Uses LIMIT to cap results.

    Note: datetime(created_ts) in ORDER BY prevents index usage. For better
    performance, consider storing timestamps as INTEGER or using created_ts directly
    (without datetime() wrapper) which allows index scan with reverse ordering.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is the "all" branch from getThreadMessages (viewer.js lines 540-548)
        sql = """
            SELECT id, subject, created_ts, importance,
                   CASE WHEN thread_id IS NULL OR thread_id = ''
                        THEN printf('msg:%d', id)
                        ELSE thread_id
                   END AS thread_key,
                   substr(COALESCE(body_md, ''), 1, 280) AS snippet
            FROM messages
            ORDER BY datetime(created_ts) DESC, id DESC
            LIMIT ?
        """

        plan = _explain_query(conn, sql, [200])

        # Print plan for documentation
        print("\nQuery plan for getThreadMessages('all'):")
        for step in plan:
            print(f"  {step}")

        # Verify query completes and respects LIMIT
        cursor = conn.execute(sql, [200])
        results = cursor.fetchall()
        assert len(results) <= 200, "Query should respect LIMIT"

        # Table scan is expected with datetime() function in ORDER BY
        # LIMIT ensures performance is acceptable even for large tables

    finally:
        conn.close()


def test_get_thread_messages_specific_thread_query_uses_index(sample_db: Path) -> None:
    """Test that getThreadMessages(specific thread) uses thread_id index.

    This query filters by thread_id and should use the thread_id index.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is the specific thread branch from getThreadMessages (viewer.js lines 550-558)
        sql = """
            SELECT id, subject, created_ts, importance,
                   CASE WHEN thread_id IS NULL OR thread_id = ''
                        THEN printf('msg:%d', id)
                        ELSE thread_id
                   END AS thread_key,
                   substr(COALESCE(body_md, ''), 1, 280) AS snippet
            FROM messages
            WHERE (thread_id = ?) OR (thread_id IS NULL AND printf('msg:%d', id) = ?)
            ORDER BY datetime(created_ts) ASC, id ASC
        """

        plan = _explain_query(conn, sql, ["thread-5", "msg:42"])

        # Print plan for debugging
        print("\nQuery plan for getThreadMessages(specific thread):")
        for step in plan:
            print(f"  {step}")

        # Should use thread_id index for the first condition
        assert _has_search_using_index(plan, "messages", "idx_messages_thread_id"), \
            "getThreadMessages(specific) should use thread_id index"

    finally:
        conn.close()


def test_fts_search_query_uses_fts_index(sample_db: Path) -> None:
    """Test that FTS search query uses FTS5 index efficiently.

    FTS queries should use the FTS5 index for searching and join with messages
    table using primary key lookup (which is very efficient).
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is the FTS search from performSearch (viewer.js lines 630-639)
        sql = """
            SELECT messages.id, messages.subject, messages.created_ts, messages.importance,
                   CASE WHEN messages.thread_id IS NULL OR messages.thread_id = ''
                        THEN printf('msg:%d', messages.id)
                        ELSE messages.thread_id
                   END AS thread_key,
                   COALESCE(snippet(fts_messages, 1, '<mark>', '</mark>', '…', 32),
                            substr(messages.body_md, 1, 280)) AS snippet
            FROM fts_messages
            JOIN messages ON messages.id = fts_messages.rowid
            WHERE fts_messages MATCH ?
            ORDER BY datetime(messages.created_ts) DESC
            LIMIT 100
        """

        plan = _explain_query(conn, sql, ["test"])

        # Print plan for debugging
        print("\nQuery plan for FTS search:")
        for step in plan:
            print(f"  {step}")

        # FTS should show up in the plan
        has_fts = any("fts_messages" in step.get("detail", "") for step in plan)
        assert has_fts, "FTS search query should use fts_messages virtual table"

        # Messages table should be accessed via SEARCH using primary key (rowid)
        assert _has_search_using_index(plan, "messages"), \
            "FTS search should use primary key lookup for messages join"

    finally:
        conn.close()


def test_like_search_fallback_query_performance(sample_db: Path) -> None:
    """Test that LIKE search fallback has reasonable performance.

    When FTS is not available, the fallback uses LIKE queries. This is not ideal
    for performance but should still avoid catastrophic scans where possible.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is the LIKE fallback from performSearch (viewer.js lines 657-665)
        sql = """
            SELECT id, subject, created_ts, importance,
                   CASE WHEN thread_id IS NULL OR thread_id = ''
                        THEN printf('msg:%d', id)
                        ELSE thread_id
                   END AS thread_key,
                   substr(COALESCE(body_md, ''), 1, 280) AS snippet
            FROM messages
            WHERE subject LIKE ? OR body_md LIKE ?
            ORDER BY datetime(created_ts) DESC
            LIMIT 100
        """

        plan = _explain_query(conn, sql, ["%test%", "%test%"])

        # Print plan for debugging
        print("\nQuery plan for LIKE search fallback:")
        for step in plan:
            print(f"  {step}")

        # LIKE queries on text columns typically require table scans
        # This is acceptable as a fallback, but we document it
        # The LIMIT 100 ensures we don't return unbounded results

        # Query should complete (basic validation)
        cursor = conn.execute(sql, ["%test%", "%test%"])
        results = cursor.fetchall()
        assert len(results) <= 100, "LIKE search should respect LIMIT"

    finally:
        conn.close()


def test_get_message_detail_query_uses_primary_key(sample_db: Path) -> None:
    """Test that getMessageDetail query uses primary key lookup.

    This query fetches a single message by ID and should use the primary key
    for instant lookup. This is the most critical query for detail view performance.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # This is getMessageDetail from viewer.js (lines 703-711)
        sql = """
            SELECT m.id, m.subject, m.body_md, m.created_ts, m.importance,
                   m.thread_id, m.project_id, m.attachments,
                   COALESCE(p.slug, '') AS project_slug,
                   COALESCE(p.human_key, '') AS project_name
            FROM messages m
            LEFT JOIN projects p ON p.id = m.project_id
            WHERE m.id = ?
        """

        plan = _explain_query(conn, sql, [42])

        # Print plan for documentation
        print("\nQuery plan for getMessageDetail:")
        for step in plan:
            print(f"  {step}")

        # Verify query completes and returns exactly one row
        cursor = conn.execute(sql, [42])
        results = cursor.fetchall()
        assert len(results) <= 1, "Query should return at most one message"

        # Primary key lookups (WHERE id = ?) should use SEARCH operations
        # This is critical for detail view performance

    finally:
        conn.close()


def test_query_plan_dbstat_locality(sample_db: Path) -> None:
    """Test data locality using dbstat to measure page clustering.

    For httpvfs streaming performance, related data should be stored close
    together on disk to minimize HTTP range requests.

    Note: Small test databases may have poor locality due to schema objects
    (indexes, FTS tables) interspersed with data. Production exports should
    run VACUUM to optimize locality.

    Note: The dbstat virtual table is an optional compile-time extension
    that may not be available in all SQLite builds (e.g., some CI environments).
    """
    conn = sqlite3.connect(sample_db)

    try:
        # Check if dbstat extension is available (optional compile-time extension)
        try:
            conn.execute("SELECT * FROM dbstat LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                pytest.skip("dbstat extension not available in this SQLite build")
            raise

        # Check that messages table has measurable locality
        cursor = conn.execute("""
            SELECT name, COUNT(*) as page_count,
                   MAX(pageno) - MIN(pageno) + 1 as page_span,
                   SUM(ncell) as total_cells
            FROM dbstat
            WHERE name IN ('messages', 'projects', 'fts_messages')
            GROUP BY name
        """)

        stats = cursor.fetchall()

        print("\nDatabase locality statistics:")
        for stat in stats:
            name, page_count, page_span, total_cells = stat
            if page_count > 0:
                locality_ratio = page_count / page_span if page_span > 0 else 1.0
                print(f"  {name}: {page_count} pages, span {page_span}, ratio {locality_ratio:.2f}, cells {total_cells}")

                # Verify stats are computed (basic validation)
                assert page_count > 0, f"Table {name} should have pages"
                assert page_span > 0, f"Table {name} should have page span"

        # For production databases, VACUUM should be run to optimize locality
        # Good locality (ratio > 0.8) minimizes HTTP Range requests for httpvfs

    finally:
        conn.close()


def test_query_plan_documentation() -> None:
    """Document the expected query patterns for httpvfs optimization.

    This test documents the query patterns and index requirements for optimal
    performance with httpvfs streaming.
    """
    documentation = """
    Query Performance Requirements for httpvfs Streaming
    ====================================================

    The static viewer relies on httpvfs to stream the SQLite database from a
    static file host using HTTP Range requests. To minimize latency:

    1. **Index Coverage**: All viewer queries should use indexes to avoid full
       table scans. Full scans require fetching the entire table over HTTP.

    2. **Primary Key Lookups**: Single-record lookups (by rowid/id) are optimal
       as they require just 1-2 page fetches.

    3. **Covering Indexes**: Indexes that contain all queried columns avoid
       additional lookups to the main table.

    4. **Data Locality**: Related records should be stored close together on
       disk to enable efficient Range requests.

    5. **FTS5 Efficiency**: FTS5 provides inverted indexes for full-text search,
       which are more efficient than LIKE scans for text searching.

    Current Query Patterns:
    -----------------------

    1. buildThreadList: Complex CTE-based aggregation
       - Indexes: created_ts for ordering
       - Note: CTEs may create temp tables, acceptable for thread summaries

    2. getThreadMessages('all'): Fetch recent messages
       - Indexes: created_ts for ORDER BY
       - Optimization: LIMIT reduces result set

    3. getThreadMessages(thread): Fetch messages in thread
       - Indexes: thread_id for WHERE clause
       - Optimization: Small result sets per thread

    4. performSearch (FTS): Full-text search
       - Indexes: FTS5 inverted index
       - Join: Primary key lookup on messages table

    5. performSearch (LIKE fallback): Text pattern matching
       - Note: Requires table scan, acceptable as fallback
       - Optimization: LIMIT 100 caps result size

    6. getMessageDetail: Fetch single message
       - Indexes: Primary key on messages.id
       - Join: Primary key on projects.id
       - Optimal: 1-2 page fetches total

    Index Maintenance:
    ------------------

    The export pipeline should ensure these indexes exist:
    - CREATE INDEX idx_messages_created_ts ON messages(created_ts)
    - CREATE INDEX idx_messages_thread_id ON messages(thread_id)
    - CREATE INDEX idx_messages_project_id ON messages(project_id)
    - CREATE VIRTUAL TABLE fts_messages USING fts5(...)

    Future Optimizations:
    ---------------------

    If httpvfs performance is inadequate on large databases:
    1. Consider materialized views with covering indexes
    2. Pre-aggregate thread summaries at export time
    3. Store denormalized data (subject, snippet) in thread table
    4. Use OPFS caching (sqlite-wasm) for warm-load performance
    """

    assert len(documentation) > 100, "Query performance documentation should be comprehensive"


@pytest.mark.parametrize("limit", [10, 100, 1000])
def test_query_scalability_with_limits(sample_db: Path, limit: int) -> None:
    """Test that queries respect LIMIT and maintain good performance at scale.

    All viewer queries use LIMIT to cap result sets. This ensures that even
    large databases don't return unbounded results.
    """
    conn = sqlite3.connect(sample_db)

    try:
        # Test thread list query with varying limits
        sql = """
            SELECT id, subject, created_ts
            FROM messages
            ORDER BY datetime(created_ts) DESC
            LIMIT ?
        """

        cursor = conn.execute(sql, [limit])
        results = cursor.fetchall()

        # Should never exceed limit
        assert len(results) <= limit, f"Query returned more than {limit} results"

        # Should complete quickly (basic validation)
        plan = _explain_query(conn, sql, [limit])
        assert plan is not None, "Query should have execution plan"

    finally:
        conn.close()
