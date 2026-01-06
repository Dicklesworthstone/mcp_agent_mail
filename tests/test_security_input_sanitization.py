"""Security Input Sanitization Tests.

Comprehensive tests for input sanitization and security measures:
1. FTS query sanitization
2. SQL injection prevention
3. Path traversal prevention
4. XSS prevention
5. Unicode/encoding attack prevention
6. Null byte injection prevention
7. Large input handling (DoS prevention)

Reference: mcp_agent_mail-yh8
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import _sanitize_fts_query, build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import Agent, Project


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


# =============================================================================
# Test: FTS Query Sanitization
# =============================================================================


class TestFTSQuerySanitization:
    """Test FTS5 query sanitization function."""

    def test_sanitize_empty_query(self):
        """Empty query returns None."""
        assert _sanitize_fts_query("") is None
        assert _sanitize_fts_query("   ") is None

    def test_sanitize_bare_wildcard(self):
        """Bare wildcards return None."""
        assert _sanitize_fts_query("*") is None
        assert _sanitize_fts_query("**") is None
        assert _sanitize_fts_query("***") is None

    def test_sanitize_bare_dots(self):
        """Bare dots return None."""
        assert _sanitize_fts_query(".") is None
        assert _sanitize_fts_query("..") is None
        assert _sanitize_fts_query("...") is None

    def test_sanitize_bare_question_marks(self):
        """Bare question marks return None."""
        assert _sanitize_fts_query("?") is None
        assert _sanitize_fts_query("??") is None
        assert _sanitize_fts_query("???") is None

    def test_sanitize_bare_boolean_operators(self):
        """Bare boolean operators return None."""
        assert _sanitize_fts_query("AND") is None
        assert _sanitize_fts_query("OR") is None
        assert _sanitize_fts_query("NOT") is None
        assert _sanitize_fts_query("and") is None
        assert _sanitize_fts_query("or") is None
        assert _sanitize_fts_query("not") is None

    def test_sanitize_leading_wildcard(self):
        """Leading wildcards are stripped."""
        assert _sanitize_fts_query("*foo") == "foo"
        assert _sanitize_fts_query("* bar") == "bar"
        assert _sanitize_fts_query("**test") == "test"

    def test_sanitize_trailing_lone_asterisk(self):
        """Trailing lone asterisks are stripped."""
        assert _sanitize_fts_query("foo *") == "foo"

    def test_sanitize_valid_prefix_pattern(self):
        """Valid prefix patterns are preserved."""
        assert _sanitize_fts_query("foo*") == "foo*"
        assert _sanitize_fts_query("test*") == "test*"

    def test_sanitize_multiple_spaces(self):
        """Multiple spaces are normalized to single space."""
        assert _sanitize_fts_query("foo  bar") == "foo bar"
        assert _sanitize_fts_query("foo   bar   baz") == "foo bar baz"

    def test_sanitize_normal_query(self):
        """Normal queries are preserved."""
        assert _sanitize_fts_query("hello world") == "hello world"
        assert _sanitize_fts_query("test") == "test"

    def test_sanitize_boolean_with_terms(self):
        """Boolean operators with terms are preserved."""
        assert _sanitize_fts_query("foo AND bar") == "foo AND bar"
        assert _sanitize_fts_query("foo OR bar") == "foo OR bar"
        assert _sanitize_fts_query("NOT foo") == "NOT foo"

    def test_sanitize_quoted_phrases(self):
        """Quoted phrases are preserved."""
        assert _sanitize_fts_query('"hello world"') == '"hello world"'
        assert _sanitize_fts_query('"test phrase"') == '"test phrase"'


# =============================================================================
# Test: SQL Injection Prevention
# =============================================================================


class TestSQLInjectionPrevention:
    """Test SQL injection prevention in queries."""

    @pytest.mark.asyncio
    async def test_search_query_sql_injection_attempt(self, isolated_env):
        """SQL injection in search query is safely handled."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        # Create a project first
        async with get_session() as session:
            project = Project(slug="sql-test", human_key="/sql/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Attempt SQL injection via search query
            malicious_queries = [
                "'; DROP TABLE messages; --",
                "1; DELETE FROM projects; --",
                "' OR '1'='1",
                "\" OR \"1\"=\"1",
                "UNION SELECT * FROM agents",
                "1; UPDATE agents SET name='hacked'",
            ]

            for malicious_query in malicious_queries:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "search_messages",
                        "arguments": {"project_key": "/sql/test", "query": malicious_query},
                    }),
                )
                # Should return 200 with empty/error result, not crash the DB
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_fts_special_characters_handled(self, isolated_env):
        """FTS special characters don't cause SQL errors."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="fts-test", human_key="/fts/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # FTS5 special characters
            special_queries = [
                "foo:bar",
                "^start",
                "near/5",
                "(grouped)",
                "{phrase}",
            ]

            for query in special_queries:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "search_messages",
                        "arguments": {"project_key": "/fts/test", "query": query},
                    }),
                )
                # May return error but should not crash
                assert response.status_code == 200


# =============================================================================
# Test: Path Traversal Prevention
# =============================================================================


class TestPathTraversalPrevention:
    """Test path traversal attack prevention."""

    @pytest.mark.asyncio
    async def test_human_key_path_traversal_attempt(self, isolated_env):
        """Path traversal in human_key is handled safely."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Attempt path traversal
            traversal_paths = [
                "../../../etc/passwd",
                "..\\..\\..\\windows\\system32",
                "/tmp/../../../etc/shadow",
                "....//....//etc/passwd",
                "%2e%2e%2f%2e%2e%2f",  # URL encoded ../
            ]

            for path in traversal_paths:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "ensure_project",
                        "arguments": {"human_key": path},
                    }),
                )
                # Should complete without exposing system files
                # The function creates the project but shouldn't access real system paths
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_file_reservation_path_traversal(self, isolated_env):
        """Path traversal in file reservation patterns is handled."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        # Create project and agent
        async with get_session() as session:
            project = Project(slug="path-test", human_key="/path/test")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(
                project_id=project.id,
                name="TestAgent",
                program="test",
                model="test",
            )
            session.add(agent)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Attempt path traversal in file patterns
            traversal_patterns = [
                ["../../../etc/passwd"],
                ["..\\..\\windows\\system32\\*"],
                ["/etc/**"],
                ["~/.ssh/*"],
            ]

            for patterns in traversal_patterns:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "file_reservation_paths",
                        "arguments": {
                            "project_key": "/path/test",
                            "agent_name": "TestAgent",
                            "paths": patterns,
                        },
                    }),
                )
                # Should complete without accessing actual system paths
                assert response.status_code == 200


# =============================================================================
# Test: Large Input Handling (DoS Prevention)
# =============================================================================


class TestLargeInputHandling:
    """Test handling of excessively large inputs."""

    @pytest.mark.asyncio
    async def test_very_long_search_query(self, isolated_env):
        """Very long search queries are handled without crash."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="large-test", human_key="/large/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Very long query (10KB)
            long_query = "a" * 10000

            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "search_messages",
                    "arguments": {"project_key": "/large/test", "query": long_query},
                }),
            )
            # Should handle gracefully
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_very_long_message_body(self, isolated_env):
        """Very long message bodies are handled without crash."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        # Create project and agent
        async with get_session() as session:
            project = Project(slug="msg-test", human_key="/msg/test")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(
                project_id=project.id,
                name="MsgAgent",
                program="test",
                model="test",
            )
            session.add(agent)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Register agent first
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "register_agent",
                    "arguments": {
                        "project_key": "/msg/test",
                        "program": "test",
                        "model": "test",
                        "name": "MsgAgent",
                    },
                }),
            )

            # Very long message body (100KB)
            long_body = "x" * 100000

            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "send_message",
                    "arguments": {
                        "project_key": "/msg/test",
                        "sender_name": "MsgAgent",
                        "to": ["MsgAgent"],
                        "subject": "Large message test",
                        "body_md": long_body,
                    },
                }),
            )
            # Should handle gracefully (may succeed or return error, but not crash)
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_many_recipients(self, isolated_env):
        """Many recipients in a message are handled without crash."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="recip-test", human_key="/recip/test")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(
                project_id=project.id,
                name="RecipAgent",
                program="test",
                model="test",
            )
            session.add(agent)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Many recipients (most won't exist)
            many_recipients = [f"Agent{i}" for i in range(100)]

            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "send_message",
                    "arguments": {
                        "project_key": "/recip/test",
                        "sender_name": "RecipAgent",
                        "to": many_recipients,
                        "subject": "Many recipients test",
                        "body_md": "Test",
                    },
                }),
            )
            # Should handle gracefully (error about unknown recipients, not crash)
            assert response.status_code == 200


# =============================================================================
# Test: Null Byte Injection Prevention
# =============================================================================


class TestNullByteInjection:
    """Test null byte injection prevention."""

    @pytest.mark.asyncio
    async def test_null_byte_in_project_key(self, isolated_env):
        """Null bytes in project key are handled safely."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "ensure_project",
                    "arguments": {"human_key": "/test/path\x00/evil"},
                }),
            )
            # Should handle without crashing
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_null_byte_in_agent_name(self, isolated_env):
        """Null bytes in agent name are handled safely."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="null-test", human_key="/null/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "register_agent",
                    "arguments": {
                        "project_key": "/null/test",
                        "program": "test",
                        "model": "test",
                        "name": "Test\x00Evil",
                    },
                }),
            )
            # Should handle - may reject invalid name or strip null
            assert response.status_code == 200


# =============================================================================
# Test: Unicode and Encoding Attacks
# =============================================================================


class TestUnicodeEncodingAttacks:
    """Test Unicode and encoding attack prevention."""

    @pytest.mark.asyncio
    async def test_unicode_normalization_in_search(self, isolated_env):
        """Unicode variations in search are handled correctly."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="unicode-test", human_key="/unicode/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Unicode variations and special characters
            unicode_queries = [
                "\u202e\u0041\u0042\u0043",  # Right-to-left override
                "test\ufeffquery",  # Zero-width no-break space
                "foo\u200bbar",  # Zero-width space
                "\u0000test",  # Null character
                "test\u001b[31m",  # ANSI escape
            ]

            for query in unicode_queries:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "search_messages",
                        "arguments": {"project_key": "/unicode/test", "query": query},
                    }),
                )
                # Should handle without crashing
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_utf8_overlong_encoding(self, isolated_env):
        """UTF-8 overlong encodings are handled safely."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="utf8-test", human_key="/utf8/test")
            session.add(project)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Mixed valid/invalid UTF-8 (Python will normalize these)
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "search_messages",
                    "arguments": {"project_key": "/utf8/test", "query": "test\uFFFDquery"},
                }),
            )
            assert response.status_code == 200


# =============================================================================
# Test: Special Characters in Identifiers
# =============================================================================


class TestSpecialCharactersInIdentifiers:
    """Test handling of special characters in identifiers."""

    @pytest.mark.asyncio
    async def test_special_chars_in_thread_id(self, isolated_env):
        """Special characters in thread_id are handled safely."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="thread-test", human_key="/thread/test")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(
                project_id=project.id,
                name="ThreadAgent",
                program="test",
                model="test",
            )
            session.add(agent)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Special characters in thread_id
            special_thread_ids = [
                "thread<script>alert(1)</script>",
                "thread'; DROP TABLE messages; --",
                "thread/../../../etc/passwd",
                "thread\x00evil",
            ]

            for thread_id in special_thread_ids:
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {
                        "name": "send_message",
                        "arguments": {
                            "project_key": "/thread/test",
                            "sender_name": "ThreadAgent",
                            "to": ["ThreadAgent"],
                            "subject": "Thread test",
                            "body_md": "Test",
                            "thread_id": thread_id,
                        },
                    }),
                )
                # Should handle without crashing (may reject or sanitize)
                assert response.status_code == 200


# =============================================================================
# Test: XSS Prevention in Message Content
# =============================================================================


class TestXSSPrevention:
    """Test XSS prevention in message content."""

    def test_xss_payloads_in_fts_sanitizer(self):
        """XSS payloads in FTS queries don't break sanitizer."""
        xss_payloads = [
            "<script>alert(1)</script>",
            "javascript:alert(1)",
            "<img src=x onerror=alert(1)>",
            "'-alert(1)-'",
            "<svg/onload=alert(1)>",
        ]

        for payload in xss_payloads:
            # Should return the payload unchanged (not None) since it's not an FTS syntax issue
            result = _sanitize_fts_query(payload)
            # The sanitizer only handles FTS syntax, not XSS - XSS prevention is elsewhere
            # Just verify it doesn't crash and returns something
            assert result is not None

    @pytest.mark.asyncio
    async def test_xss_in_message_body_stored_safely(self, isolated_env):
        """XSS payloads in message body are stored without execution."""
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        async with get_session() as session:
            project = Project(slug="xss-test", human_key="/xss/test")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(
                project_id=project.id,
                name="XSSAgent",
                program="test",
                model="test",
            )
            session.add(agent)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            xss_body = "<script>alert('XSS')</script>"

            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {
                    "name": "send_message",
                    "arguments": {
                        "project_key": "/xss/test",
                        "sender_name": "XSSAgent",
                        "to": ["XSSAgent"],
                        "subject": "XSS Test",
                        "body_md": xss_body,
                    },
                }),
            )
            # Should store successfully (escaping happens on display, not storage)
            assert response.status_code == 200


# =============================================================================
# Test: Malformed JSON Handling
# =============================================================================


class TestMalformedJSONHandling:
    """Test handling of malformed JSON in requests."""

    @pytest.mark.asyncio
    async def test_deeply_nested_json(self, isolated_env):
        """Deeply nested JSON doesn't cause stack overflow."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Deeply nested structure (100 levels)
            nested = {"level": None}
            current = nested
            for _ in range(100):
                current["level"] = {"level": None}
                current = current["level"]

            response = await client.post(
                settings.http.path,
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "tools/call",
                    "params": {"name": "health_check", "arguments": nested},
                },
            )
            # Should handle without crashing
            assert response.status_code in (200, 400)

    @pytest.mark.asyncio
    async def test_json_with_duplicate_keys(self, isolated_env):
        """JSON with duplicate keys is handled safely."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Note: Python's json module takes the last value for duplicate keys
            response = await client.post(
                settings.http.path,
                content=b'{"jsonrpc":"2.0","id":"1","id":"2","method":"tools/call","params":{"name":"health_check","arguments":{}}}',
                headers={"Content-Type": "application/json"},
            )
            # Should handle without crashing
            assert response.status_code in (200, 400)
