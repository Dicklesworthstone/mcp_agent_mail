"""Full-text search helpers for database-agnostic FTS operations.

This module provides abstractions for full-text search that work with both
SQLite FTS5 and PostgreSQL tsvector/GIN indexes. It centralizes:
- Database type detection
- Query sanitization and translation
- SQL generation for search queries
- Snippet and ranking expressions
"""

from __future__ import annotations

import re
from typing import Literal

# Database type literals
DbType = Literal["sqlite", "postgresql"]

# Patterns that are unsearchable - return None to signal "no results"
_UNSEARCHABLE_PATTERNS = frozenset({"*", "**", "***", ".", "..", "...", "?", "??", "???", ""})


def get_db_type(url: str) -> DbType:
    """Determine database type from a DATABASE_URL string.

    Args:
        url: Database URL (e.g., "sqlite+aiosqlite:///./storage.sqlite3" or
             "postgresql+asyncpg://user:pass@host/db")

    Returns:
        "sqlite" or "postgresql"

    Raises:
        ValueError: If the database type cannot be determined or is unsupported.
    """
    url_lower = url.lower()
    if "sqlite" in url_lower:
        return "sqlite"
    if "postgresql" in url_lower or "postgres" in url_lower:
        return "postgresql"
    raise ValueError(f"Unsupported database type in URL: {url}")


def sanitize_fts_query(query: str, db_type: DbType) -> str | None:
    """Sanitize and prepare an FTS query for the target database.

    For SQLite: Validates FTS5 syntax, fixes common issues.
    For PostgreSQL: Converts FTS5-style syntax to tsquery format.

    Args:
        query: The user's search query string.
        db_type: Target database type.

    Returns:
        Sanitized query string ready for execution, or None if the query
        cannot produce meaningful results (caller should return empty list).
    """
    if not query:
        return None

    trimmed = query.strip()
    if not trimmed:
        return None

    # Check for bare patterns that can't match anything meaningful
    if trimmed in _UNSEARCHABLE_PATTERNS:
        return None

    # Bare boolean operators without terms - can't search
    upper_trimmed = trimmed.upper()
    if upper_trimmed in {"AND", "OR", "NOT"}:
        return None

    if db_type == "sqlite":
        return _sanitize_fts5_query(trimmed)
    else:
        return _sanitize_postgresql_query(trimmed)


def _sanitize_fts5_query(query: str) -> str | None:
    """Sanitize a query for SQLite FTS5.

    FTS5 has specific syntax requirements. This function attempts to fix
    common mistakes rather than throwing errors.

    Fixes applied:
    - Strips whitespace
    - Removes leading bare `*` (keeps `term*` prefix patterns)
    - Converts unsearchable patterns to None (empty results)
    """
    trimmed = query

    # FTS5 doesn't support leading wildcards (*foo), only trailing (foo*).
    # Strip leading "*" regardless of what follows: "*foo" -> "foo", "* bar" -> "bar"
    if trimmed.startswith("*"):
        if len(trimmed) == 1:
            return None
        # Strip leading "*" (and any following whitespace) and recurse
        return _sanitize_fts5_query(trimmed[1:].lstrip())

    # Fix trailing lone asterisks that aren't part of prefix patterns
    # e.g., "foo *" -> "foo"
    if trimmed.endswith(" *"):
        trimmed = trimmed[:-2].rstrip()
        if not trimmed:
            return None

    # Multiple consecutive spaces -> single space
    while "  " in trimmed:
        trimmed = trimmed.replace("  ", " ")

    return trimmed if trimmed else None


def _sanitize_postgresql_query(query: str) -> str | None:
    """Sanitize and convert a query for PostgreSQL tsquery.

    Converts FTS5-style syntax to PostgreSQL websearch_to_tsquery compatible format.
    PostgreSQL's websearch_to_tsquery handles most common search patterns naturally.

    Syntax mapping:
    - Simple terms: "hello world" -> websearch handles naturally
    - Quoted phrases: "hello world" -> websearch handles naturally
    - AND/OR: handled by websearch_to_tsquery
    - Prefix: "term*" -> needs special handling with to_tsquery
    """
    trimmed = query

    # Strip leading wildcards (not supported)
    if trimmed.startswith("*"):
        if len(trimmed) == 1:
            return None
        return _sanitize_postgresql_query(trimmed[1:].lstrip())

    # Multiple consecutive spaces -> single space
    while "  " in trimmed:
        trimmed = trimmed.replace("  ", " ")

    return trimmed if trimmed else None


def fts5_to_tsquery(fts5_query: str) -> str:
    """Convert FTS5 query syntax to PostgreSQL tsquery syntax.

    This handles the conversion of common FTS5 patterns to their PostgreSQL
    equivalents for use with to_tsquery().

    Mappings:
    - term -> term
    - term* -> term:*
    - "phrase" -> phrase (use plainto_tsquery for phrases)
    - AND -> &
    - OR -> |
    - NOT -> !

    Args:
        fts5_query: Query in FTS5 syntax.

    Returns:
        Query converted to tsquery syntax.
    """
    if not fts5_query:
        return ""

    result = fts5_query

    # Handle prefix wildcards: term* -> term:*
    result = re.sub(r'(\w+)\*', r'\1:*', result)

    # Handle boolean operators (case-insensitive)
    result = re.sub(r'\bAND\b', '&', result, flags=re.IGNORECASE)
    result = re.sub(r'\bOR\b', '|', result, flags=re.IGNORECASE)
    result = re.sub(r'\bNOT\b', '!', result, flags=re.IGNORECASE)

    # Clean up spacing around operators
    result = re.sub(r'\s*&\s*', ' & ', result)
    result = re.sub(r'\s*\|\s*', ' | ', result)
    result = re.sub(r'\s*!\s*', ' ! ', result)

    return result.strip()


def build_search_query_sql(
    db_type: DbType,
    *,
    table_alias: str = "m",
    project_filter: str = "project_id = :project_id",
    include_snippet: bool = False,
    snippet_column: str = "body_md",
    snippet_length: int = 18,
    order_by: str = "relevance",
    bm25_weights: tuple[float, float, float] = (0.0, 1.0, 1.0),
    limit: int = 20,
) -> str:
    """Build a complete FTS search SQL query for the given database type.

    Args:
        db_type: Target database ("sqlite" or "postgresql").
        table_alias: Alias for the messages table (default "m").
        project_filter: WHERE clause for project filtering.
        include_snippet: Whether to include snippet extraction.
        snippet_column: Column to extract snippets from.
        snippet_length: Max words in snippet.
        order_by: "relevance" for FTS ranking, "time" for created_ts DESC.
        bm25_weights: Weights for BM25 ranking (message_id, subject, body).
        limit: Max results to return.

    Returns:
        Complete SQL query string with :query and :project_id parameters.
    """
    if db_type == "sqlite":
        return _build_sqlite_search_sql(
            table_alias=table_alias,
            project_filter=project_filter,
            include_snippet=include_snippet,
            snippet_length=snippet_length,
            order_by=order_by,
            bm25_weights=bm25_weights,
            limit=limit,
        )
    else:
        return _build_postgresql_search_sql(
            table_alias=table_alias,
            project_filter=project_filter,
            include_snippet=include_snippet,
            snippet_column=snippet_column,
            snippet_length=snippet_length,
            order_by=order_by,
            limit=limit,
        )


def _build_sqlite_search_sql(
    *,
    table_alias: str,
    project_filter: str,
    include_snippet: bool,
    snippet_length: int,
    order_by: str,
    bm25_weights: tuple[float, float, float],
    limit: int,
) -> str:
    """Build SQLite FTS5 search query."""
    # Base SELECT columns
    select_cols = [
        f"{table_alias}.id",
        f"{table_alias}.subject",
        f"{table_alias}.body_md",
        f"{table_alias}.importance",
        f"{table_alias}.ack_required",
        f"{table_alias}.created_ts",
        f"{table_alias}.thread_id",
        "a.name AS sender_name",
    ]

    if include_snippet:
        # snippet(fts_messages, column_index, start_mark, end_mark, ellipsis, max_tokens)
        # Column 2 is body in our FTS5 table (0=message_id, 1=subject, 2=body)
        select_cols.append(
            f"snippet(fts_messages, 2, '<mark>', '</mark>', '...', {snippet_length}) AS body_snippet"
        )

    # Build ORDER BY clause
    if order_by == "time":
        order_clause = f"ORDER BY {table_alias}.created_ts DESC"
    else:
        # BM25 ranking (lower is better in SQLite FTS5)
        w0, w1, w2 = bm25_weights
        order_clause = f"ORDER BY bm25(fts_messages, {w0}, {w1}, {w2}) ASC"

    sql = f"""
        SELECT {', '.join(select_cols)}
        FROM fts_messages
        JOIN messages {table_alias} ON fts_messages.rowid = {table_alias}.id
        JOIN agents a ON {table_alias}.sender_id = a.id
        WHERE {table_alias}.{project_filter} AND fts_messages MATCH :query
        {order_clause}
        LIMIT {limit}
    """
    return sql.strip()


def _build_postgresql_search_sql(
    *,
    table_alias: str,
    project_filter: str,
    include_snippet: bool,
    snippet_column: str,
    snippet_length: int,
    order_by: str,
    limit: int,
) -> str:
    """Build PostgreSQL tsvector search query."""
    # Base SELECT columns
    select_cols = [
        f"{table_alias}.id",
        f"{table_alias}.subject",
        f"{table_alias}.body_md",
        f"{table_alias}.importance",
        f"{table_alias}.ack_required",
        f"{table_alias}.created_ts",
        f"{table_alias}.thread_id",
        "a.name AS sender_name",
    ]

    if include_snippet:
        # ts_headline for snippet extraction
        # MaxWords controls snippet length, MinWords ensures minimum context
        select_cols.append(
            f"ts_headline('english', {table_alias}.{snippet_column}, websearch_to_tsquery('english', :query), "
            f"'StartSel=<mark>, StopSel=</mark>, MaxWords={snippet_length}, MinWords=5, ShortWord=3') AS body_snippet"
        )

    # Build ORDER BY clause
    if order_by == "time":
        order_clause = f"ORDER BY {table_alias}.created_ts DESC"
    else:
        # ts_rank for relevance (higher is better, so DESC)
        order_clause = f"ORDER BY ts_rank({table_alias}.search_vector, websearch_to_tsquery('english', :query)) DESC"

    sql = f"""
        SELECT {', '.join(select_cols)}
        FROM messages {table_alias}
        JOIN agents a ON {table_alias}.sender_id = a.id
        WHERE {table_alias}.{project_filter}
          AND {table_alias}.search_vector @@ websearch_to_tsquery('english', :query)
        {order_clause}
        LIMIT {limit}
    """
    return sql.strip()


def build_snippet_expression(
    db_type: DbType,
    column_name: str = "body_md",
    snippet_length: int = 18,
    start_marker: str = "<mark>",
    end_marker: str = "</mark>",
) -> str:
    """Build a snippet extraction SQL expression.

    Args:
        db_type: Target database type.
        column_name: Column to extract snippet from.
        snippet_length: Maximum words in snippet.
        start_marker: HTML tag to mark match start.
        end_marker: HTML tag to mark match end.

    Returns:
        SQL expression that extracts a highlighted snippet.
    """
    if db_type == "sqlite":
        # snippet(fts_table, column_index, start, end, ellipsis, max_tokens)
        # Assumes column 2 is body in fts_messages
        return f"snippet(fts_messages, 2, '{start_marker}', '{end_marker}', '...', {snippet_length})"
    else:
        # ts_headline(config, text, query, options)
        return (
            f"ts_headline('english', {column_name}, websearch_to_tsquery('english', :query), "
            f"'StartSel={start_marker}, StopSel={end_marker}, MaxWords={snippet_length}, MinWords=5, ShortWord=3')"
        )


def build_rank_expression(
    db_type: DbType,
    weights: tuple[float, float, float] | None = None,
) -> str:
    """Build a ranking expression for ORDER BY.

    Args:
        db_type: Target database type.
        weights: Optional BM25 weights for SQLite (message_id, subject, body).

    Returns:
        SQL expression for ranking, suitable for ORDER BY.
    """
    if db_type == "sqlite":
        if weights:
            w0, w1, w2 = weights
            return f"bm25(fts_messages, {w0}, {w1}, {w2})"
        return "bm25(fts_messages)"
    else:
        return "ts_rank(search_vector, websearch_to_tsquery('english', :query))"


def build_match_condition(db_type: DbType, table_alias: str = "m") -> str:
    """Build the FTS match condition for WHERE clause.

    Args:
        db_type: Target database type.
        table_alias: Alias for the messages table.

    Returns:
        SQL condition for FTS matching with :query parameter.
    """
    if db_type == "sqlite":
        return "fts_messages MATCH :query"
    else:
        return f"{table_alias}.search_vector @@ websearch_to_tsquery('english', :query)"
