"""Unit tests for FTS helper functions.

These tests don't require a database connection - they test the pure
functions for database type detection, query sanitization, query
translation, and SQL generation.
"""

from __future__ import annotations

import pytest

from mcp_agent_mail.fts_helpers import (
    build_match_condition,
    build_rank_expression,
    build_search_query_sql,
    build_snippet_expression,
    fts5_to_tsquery,
    get_db_type,
    sanitize_fts_query,
)


class TestGetDbType:
    """Tests for get_db_type() function."""

    def test_sqlite_aiosqlite(self):
        url = "sqlite+aiosqlite:///./storage.sqlite3"
        assert get_db_type(url) == "sqlite"

    def test_sqlite_plain(self):
        url = "sqlite:///./storage.sqlite3"
        assert get_db_type(url) == "sqlite"

    def test_postgresql_asyncpg(self):
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        assert get_db_type(url) == "postgresql"

    def test_postgres_asyncpg(self):
        # postgres:// is also valid
        url = "postgres+asyncpg://user:pass@host/db"
        assert get_db_type(url) == "postgresql"

    def test_postgresql_plain(self):
        url = "postgresql://user:pass@host/db"
        assert get_db_type(url) == "postgresql"

    def test_case_insensitive(self):
        url = "POSTGRESQL+ASYNCPG://user:pass@host/db"
        assert get_db_type(url) == "postgresql"

    def test_unsupported_database(self):
        with pytest.raises(ValueError, match="Unsupported database type"):
            get_db_type("mysql://user:pass@host/db")


class TestSanitizeFtsQuery:
    """Tests for sanitize_fts_query() function."""

    def test_empty_string(self):
        assert sanitize_fts_query("", "sqlite") is None
        assert sanitize_fts_query("", "postgresql") is None

    def test_whitespace_only(self):
        assert sanitize_fts_query("   ", "sqlite") is None
        assert sanitize_fts_query("\t\n", "postgresql") is None

    def test_bare_wildcard(self):
        # Bare wildcards can't match anything meaningful
        assert sanitize_fts_query("*", "sqlite") is None
        assert sanitize_fts_query("**", "postgresql") is None
        assert sanitize_fts_query("***", "sqlite") is None

    def test_bare_boolean_operators(self):
        assert sanitize_fts_query("AND", "sqlite") is None
        assert sanitize_fts_query("OR", "postgresql") is None
        assert sanitize_fts_query("NOT", "sqlite") is None

    def test_leading_wildcard_stripped(self):
        # FTS5 doesn't support leading wildcards
        assert sanitize_fts_query("*foo", "sqlite") == "foo"
        assert sanitize_fts_query("*foo", "postgresql") == "foo"

    def test_leading_wildcard_with_space(self):
        assert sanitize_fts_query("* bar", "sqlite") == "bar"

    def test_trailing_lone_asterisk_stripped(self):
        assert sanitize_fts_query("foo *", "sqlite") == "foo"

    def test_valid_query_unchanged(self):
        assert sanitize_fts_query("hello world", "sqlite") == "hello world"
        assert sanitize_fts_query("hello world", "postgresql") == "hello world"

    def test_prefix_pattern_preserved(self):
        # term* patterns should work
        assert sanitize_fts_query("hello*", "sqlite") == "hello*"
        assert sanitize_fts_query("hello*", "postgresql") == "hello*"

    def test_multiple_spaces_normalized(self):
        assert sanitize_fts_query("foo  bar", "sqlite") == "foo bar"
        assert sanitize_fts_query("foo   bar   baz", "postgresql") == "foo bar baz"


class TestFts5ToTsquery:
    """Tests for fts5_to_tsquery() conversion function."""

    def test_simple_term(self):
        assert fts5_to_tsquery("hello") == "hello"

    def test_prefix_pattern(self):
        # term* -> term:*
        assert fts5_to_tsquery("hello*") == "hello:*"

    def test_and_operator(self):
        assert fts5_to_tsquery("foo AND bar") == "foo & bar"

    def test_or_operator(self):
        assert fts5_to_tsquery("foo OR bar") == "foo | bar"

    def test_not_operator(self):
        assert fts5_to_tsquery("NOT foo") == "! foo"

    def test_complex_query(self):
        result = fts5_to_tsquery("hello* AND world OR foo")
        assert "hello:*" in result
        assert "&" in result
        assert "|" in result

    def test_case_insensitive_operators(self):
        assert fts5_to_tsquery("foo and bar") == "foo & bar"
        assert fts5_to_tsquery("foo or bar") == "foo | bar"

    def test_empty_string(self):
        assert fts5_to_tsquery("") == ""


class TestBuildSearchQuerySql:
    """Tests for build_search_query_sql() function."""

    def test_sqlite_basic(self):
        sql = build_search_query_sql("sqlite", table_alias="m", include_snippet=False)
        # Should reference fts_messages and MATCH
        assert "fts_messages" in sql
        assert "MATCH :query" in sql
        assert "bm25(fts_messages" in sql
        # Should select expected columns
        assert "m.id" in sql
        assert "m.subject" in sql

    def test_postgresql_basic(self):
        sql = build_search_query_sql("postgresql", table_alias="m", include_snippet=False)
        # Should NOT reference fts_messages
        assert "fts_messages" not in sql
        # Should use tsvector
        assert "search_vector" in sql
        assert "websearch_to_tsquery" in sql
        assert "ts_rank" in sql

    def test_sqlite_with_snippet(self):
        sql = build_search_query_sql("sqlite", include_snippet=True)
        assert "snippet(fts_messages" in sql
        assert "body_snippet" in sql

    def test_postgresql_with_snippet(self):
        sql = build_search_query_sql("postgresql", include_snippet=True)
        assert "ts_headline" in sql
        assert "body_snippet" in sql

    def test_order_by_time(self):
        sql = build_search_query_sql("sqlite", order_by="time")
        assert "ORDER BY" in sql
        assert "created_ts DESC" in sql

    def test_order_by_relevance_sqlite(self):
        sql = build_search_query_sql("sqlite", order_by="relevance")
        assert "bm25(fts_messages" in sql

    def test_order_by_relevance_postgresql(self):
        sql = build_search_query_sql("postgresql", order_by="relevance")
        assert "ts_rank" in sql
        assert "DESC" in sql

    def test_limit(self):
        sql = build_search_query_sql("sqlite", limit=50)
        assert "LIMIT 50" in sql

    def test_project_filter(self):
        sql = build_search_query_sql("sqlite", project_filter="project_id IN :ids")
        assert "project_id IN :ids" in sql

    def test_custom_weights(self):
        sql = build_search_query_sql(
            "sqlite", bm25_weights=(0.0, 2.0, 1.0), order_by="relevance"
        )
        assert "bm25(fts_messages, 0.0, 2.0, 1.0)" in sql


class TestBuildSnippetExpression:
    """Tests for build_snippet_expression() function."""

    def test_sqlite(self):
        expr = build_snippet_expression("sqlite", snippet_length=20)
        assert "snippet(fts_messages" in expr
        assert "20" in expr
        assert "<mark>" in expr

    def test_postgresql(self):
        expr = build_snippet_expression("postgresql", column_name="body_md", snippet_length=20)
        assert "ts_headline" in expr
        assert "body_md" in expr
        assert "MaxWords=20" in expr

    def test_custom_markers(self):
        expr = build_snippet_expression(
            "sqlite", start_marker="<b>", end_marker="</b>"
        )
        assert "<b>" in expr
        assert "</b>" in expr


class TestBuildRankExpression:
    """Tests for build_rank_expression() function."""

    def test_sqlite_default(self):
        expr = build_rank_expression("sqlite")
        assert "bm25(fts_messages)" in expr

    def test_sqlite_with_weights(self):
        expr = build_rank_expression("sqlite", weights=(0.0, 3.0, 1.0))
        assert "bm25(fts_messages, 0.0, 3.0, 1.0)" in expr

    def test_postgresql(self):
        expr = build_rank_expression("postgresql")
        assert "ts_rank" in expr
        assert "search_vector" in expr


class TestBuildMatchCondition:
    """Tests for build_match_condition() function."""

    def test_sqlite(self):
        cond = build_match_condition("sqlite", table_alias="m")
        assert "fts_messages MATCH :query" in cond

    def test_postgresql(self):
        cond = build_match_condition("postgresql", table_alias="m")
        assert "m.search_vector" in cond
        assert "@@" in cond
        assert "websearch_to_tsquery" in cond
