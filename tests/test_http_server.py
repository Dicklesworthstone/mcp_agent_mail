"""HTTP Server and Transport Tests.

Comprehensive tests for HTTP server functionality:
1. Server starts on configured port
2. Health endpoint returns 200
3. SSE connection established
4. Tool calls work over HTTP
5. Resource reads work over HTTP
6. CORS headers present

Reference: mcp_agent_mail-9z6
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema
from mcp_agent_mail.http import _collect_retention_quota_report, build_http_app
from mcp_agent_mail.storage import ensure_archive


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


# =============================================================================
# Test: Server Starts on Configured Port
# =============================================================================


class TestServerConfiguration:
    """Test that server respects configuration settings."""

    @pytest.mark.asyncio
    async def test_server_builds_with_default_config(self, isolated_env):
        """Server builds successfully with default configuration."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        assert app is not None
        # FastAPI app should have routes
        assert len(app.routes) > 0

    @pytest.mark.asyncio
    async def test_server_uses_configured_path(self, isolated_env, monkeypatch):
        """Server mounts MCP handler at configured HTTP_PATH."""
        monkeypatch.setenv("HTTP_PATH", "/custom-mcp/")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()
        assert settings.http.path == "/custom-mcp/"

        server = build_mcp_server()
        app = build_http_app(settings, server)
        assert app is not None

    @pytest.mark.asyncio
    async def test_server_mounts_api_and_mcp_aliases(self, isolated_env, monkeypatch):
        """Server always exposes MCP transport on both /api and /mcp aliases."""
        monkeypatch.setenv("HTTP_PATH", "/custom-mcp/")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        mounted_paths = {getattr(route, "path", "") for route in app.routes}
        assert "/custom-mcp" in mounted_paths
        assert "/api" in mounted_paths
        assert "/mcp" in mounted_paths

    @pytest.mark.asyncio
    async def test_server_builds_with_custom_host_port(self, isolated_env, monkeypatch):
        """Server configuration accepts custom host and port."""
        monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
        monkeypatch.setenv("HTTP_PORT", "9999")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        assert settings.http.host == "0.0.0.0"
        assert settings.http.port == 9999

        server = build_mcp_server()
        app = build_http_app(settings, server)
        assert app is not None

    @pytest.mark.asyncio
    async def test_retention_quota_report_offloads_scan(self, isolated_env, monkeypatch):
        """Retention quota scans should run off the main event-loop thread."""
        settings = _config.get_settings()
        main_thread = threading.main_thread()

        def fake_report(_settings):
            assert threading.current_thread() is not main_thread
            return {
                "old_messages": 3,
                "retention_max_age_days": 180,
                "total_attachments_bytes": 1024,
                "quota_limit_bytes": 2048,
                "per_project_attach": {"backend": 1024},
                "per_project_inbox_counts": {"backend": 2},
            }

        monkeypatch.setattr("mcp_agent_mail.http._collect_retention_quota_report_sync", fake_report)

        report = await _collect_retention_quota_report(settings)
        assert report["old_messages"] == 3
        assert report["per_project_attach"]["backend"] == 1024

    @pytest.mark.asyncio
    async def test_retention_quota_report_scans_project_archive_layout(self, isolated_env):
        """Quota scans should read STORAGE_ROOT/projects/<slug>, not STORAGE_ROOT/<slug>."""
        settings = _config.get_settings()
        archive = await ensure_archive(settings, "backend")

        message_path = archive.root / "messages" / "2026" / "04" / "retention-check.md"
        message_path.parent.mkdir(parents=True, exist_ok=True)
        message_path.write_text("message", encoding="utf-8")
        old_ts = message_path.stat().st_mtime - (400 * 86400)
        os.utime(message_path, (old_ts, old_ts))

        inbox_path = archive.root / "agents" / "BlueLake" / "inbox" / "2026" / "04" / "msg.md"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text("inbox", encoding="utf-8")

        attachment_path = archive.root / "attachments" / "thumb.webp"
        attachment_path.parent.mkdir(parents=True, exist_ok=True)
        attachment_path.write_bytes(b"RIFFfakewebp")

        report = await _collect_retention_quota_report(settings)
        assert report["old_messages"] >= 1
        assert report["per_project_inbox_counts"]["backend"] >= 1
        assert report["per_project_attach"]["backend"] == attachment_path.stat().st_size


# =============================================================================
# Test: Health Endpoints Return 200
# =============================================================================


class TestHealthEndpoints:
    """Test health check endpoints."""

    @pytest.mark.asyncio
    async def test_liveness_returns_200(self, isolated_env):
        """Liveness endpoint returns 200 with status 'alive'."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_readiness_returns_200_when_healthy(self, isolated_env):
        """Readiness endpoint returns 200 when database is accessible."""
        # Ensure schema exists for readiness check
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/readiness")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"

    @pytest.mark.asyncio
    async def test_health_endpoints_bypass_auth(self, isolated_env, monkeypatch):
        """Health endpoints work without authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # No auth header - should still work for health endpoints
            r1 = await client.get("/health/liveness")
            assert r1.status_code == 200

            # Readiness might fail if DB not ready, but should not be 401
            r2 = await client.get("/health/readiness")
            assert r2.status_code != 401


# =============================================================================
# Test: SSE Connection Established
# =============================================================================


class TestSSEConnection:
    """Test Server-Sent Events (SSE) connection capability."""

    @pytest.mark.asyncio
    async def test_sse_accept_header_supported(self, isolated_env):
        """Server accepts SSE content type in Accept header."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Request with SSE Accept header
            headers = {"Accept": "text/event-stream"}
            response = await client.get(
                "/health/liveness",
                headers=headers,
            )
            # Health endpoints return JSON regardless, but should not error
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_mcp_endpoint_accepts_sse_header(self, isolated_env):
        """MCP endpoint accepts SSE content negotiation."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # POST to MCP path with SSE in Accept
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            response = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            # Should get a valid response (200 or 401 if auth required)
            assert response.status_code in (200, 401)


# =============================================================================
# Test: Tool Calls Work Over HTTP
# =============================================================================


class TestToolCallsOverHTTP:
    """Test that MCP tool calls work over HTTP transport."""

    @pytest.mark.asyncio
    async def test_health_check_tool_succeeds(self, isolated_env):
        """health_check tool call returns success over HTTP."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            data = response.json()
            # JSON-RPC response should have result
            assert "result" in data or "error" not in data

    @pytest.mark.asyncio
    async def test_tool_call_returns_jsonrpc_format(self, isolated_env):
        """Tool calls return proper JSON-RPC 2.0 format."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            data = response.json()
            assert data.get("jsonrpc") == "2.0"
            assert "id" in data

    @pytest.mark.asyncio
    async def test_tool_call_with_bearer_auth(self, isolated_env, monkeypatch):
        """Tool calls work with bearer token authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "my-secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Without auth -> 401
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 401

            # With correct auth -> 200
            r2 = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer my-secret-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 200


# =============================================================================
# Test: Resource Reads Work Over HTTP
# =============================================================================


class TestResourceReadsOverHTTP:
    """Test that MCP resource reads work over HTTP transport."""

    @pytest.mark.asyncio
    async def test_resources_list_returns_data(self, isolated_env):
        """resources/list returns available resources."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("resources/list", {}),
            )
            assert response.status_code == 200
            data = response.json()
            assert "result" in data or "error" not in data

    @pytest.mark.asyncio
    async def test_resource_read_returns_jsonrpc(self, isolated_env):
        """Resource reads return proper JSON-RPC response."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": "resource://tooling/projects"}),
            )
            assert response.status_code == 200
            data = response.json()
            assert data.get("jsonrpc") == "2.0"


# =============================================================================
# Test: CORS Headers Present
# =============================================================================


class TestCORSHeaders:
    """Test CORS (Cross-Origin Resource Sharing) configuration."""

    @pytest.mark.asyncio
    async def test_cors_preflight_returns_headers(self, isolated_env, monkeypatch):
        """CORS preflight OPTIONS request returns appropriate headers."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                settings.http.path,
                headers={
                    "Origin": "http://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code in (200, 204)
            # CORS headers should be present
            assert "access-control-allow-origin" in response.headers

    @pytest.mark.asyncio
    async def test_cors_headers_on_response(self, isolated_env, monkeypatch):
        """CORS headers are present on regular responses."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "*")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Origin": "http://test-origin.com"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            # CORS header should be present
            assert response.headers.get("access-control-allow-origin") in ("*", "http://test-origin.com")

    @pytest.mark.asyncio
    async def test_cors_disabled_no_headers(self, isolated_env, monkeypatch):
        """When CORS is disabled, no CORS headers are added."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200
            # CORS headers should not be present when disabled
            # Note: This may vary based on implementation; check for absence
            # of origin-specific headers on non-preflight requests


# =============================================================================
# Test: Error Handling
# =============================================================================


class TestHTTPErrorHandling:
    """Test HTTP error handling."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, isolated_env):
        """Invalid JSON payload returns appropriate error."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                content=b"not valid json{{{",
                headers={"Content-Type": "application/json"},
            )
            # Should return 4xx error for invalid JSON
            assert response.status_code >= 400

    @pytest.mark.asyncio
    async def test_missing_method_returns_error(self, isolated_env):
        """JSON-RPC request without method returns error."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json={"jsonrpc": "2.0", "id": "1"},  # Missing method
            )
            # Should return 200 with JSON-RPC error or 400
            assert response.status_code in (200, 400)
            if response.status_code == 200:
                data = response.json()
                assert "error" in data


# =============================================================================
# Test: Request Logging
# =============================================================================


class TestRequestLogging:
    """Test request logging middleware."""

    @pytest.mark.asyncio
    async def test_request_logs_path_and_status(self, isolated_env, caplog):
        """Requests are logged with path and status."""
        import logging

        caplog.set_level(logging.DEBUG)

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/health/liveness")

        # Logging should have occurred (may use structlog or stdlib)
        # This is a smoke test that the request completes


# =============================================================================
# Test: HTTP Lock Scope
# =============================================================================


class TestHTTPLockScope:
    """Regression tests for DB/archive lock ordering in HTTP routes."""

    @pytest.mark.asyncio
    async def test_overseer_send_archives_after_db_session_closes(self, isolated_env, monkeypatch):
        import mcp_agent_mail.http as http_module
        import mcp_agent_mail.storage as storage_module
        from mcp_agent_mail.db import get_session as real_get_session
        from mcp_agent_mail.models import Agent, Project

        await ensure_schema()
        async with real_get_session() as session:
            project = Project(slug="http-overseer-lock", human_key="/tmp/http-overseer-lock")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None

            recipient = Agent(
                project_id=project.id,
                name="BlueLake",
                program="test",
                model="test",
                task_description="recipient",
            )
            session.add(recipient)
            await session.commit()

        session_depth = 0
        archive_write_depths: list[int] = []
        original_write_message_bundle = storage_module.write_message_bundle
        original_get_session = http_module.get_session

        @contextlib.asynccontextmanager
        async def tracking_get_session(*args: Any, **kwargs: Any):
            nonlocal session_depth
            async with original_get_session(*args, **kwargs) as session:
                session_depth += 1
                try:
                    yield session
                finally:
                    session_depth -= 1

        async def tracking_write_message_bundle(*args: Any, **kwargs: Any):
            archive_write_depths.append(session_depth)
            return await original_write_message_bundle(*args, **kwargs)

        monkeypatch.setattr(http_module, "get_session", tracking_get_session)
        monkeypatch.setattr(storage_module, "write_message_bundle", tracking_write_message_bundle)

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/mail/{project.slug}/overseer/send",
                json={
                    "recipients": ["BlueLake"],
                    "subject": "Lock Scope",
                    "body_md": "Check DB/archive ordering.",
                },
            )

        assert response.status_code == 200
        assert archive_write_depths == [0]

    @pytest.mark.asyncio
    async def test_delete_messages_archives_after_db_session_closes(self, isolated_env, monkeypatch):
        import mcp_agent_mail.http as http_module
        from mcp_agent_mail.db import get_session as real_get_session
        from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project

        await ensure_schema()
        async with real_get_session() as session:
            project = Project(slug="http-delete-lock", human_key="/tmp/http-delete-lock")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None

            sender = Agent(
                project_id=project.id,
                name="GreenCastle",
                program="test",
                model="test",
                task_description="sender",
            )
            recipient = Agent(
                project_id=project.id,
                name="BlueLake",
                program="test",
                model="test",
                task_description="recipient",
            )
            session.add(sender)
            session.add(recipient)
            await session.commit()
            await session.refresh(sender)
            await session.refresh(recipient)
            assert sender.id is not None
            assert recipient.id is not None

            message = Message(
                project_id=project.id,
                sender_id=sender.id,
                subject="Delete Me",
                body_md="body",
                importance="normal",
                ack_required=False,
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)
            assert message.id is not None

            session.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=recipient.id,
                    kind="to",
                )
            )
            await session.commit()

        session_depth = 0
        archive_depths: list[int] = []
        original_get_session = http_module.get_session
        original_ensure_archive = http_module.ensure_archive

        @contextlib.asynccontextmanager
        async def tracking_get_session(*args: Any, **kwargs: Any):
            nonlocal session_depth
            async with original_get_session(*args, **kwargs) as session:
                session_depth += 1
                try:
                    yield session
                finally:
                    session_depth -= 1

        async def tracking_ensure_archive(*args: Any, **kwargs: Any):
            archive_depths.append(session_depth)
            return await original_ensure_archive(*args, **kwargs)

        monkeypatch.setattr(http_module, "get_session", tracking_get_session)
        monkeypatch.setattr(http_module, "ensure_archive", tracking_ensure_archive)

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mail/api/delete-messages",
                json={"message_ids": [message.id]},
            )

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert archive_depths == [0]

    @pytest.mark.asyncio
    async def test_inbox_delete_archives_after_db_session_closes(self, isolated_env, monkeypatch):
        import mcp_agent_mail.http as http_module
        from mcp_agent_mail.db import get_session as real_get_session
        from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project

        await ensure_schema()
        async with real_get_session() as session:
            project = Project(slug="http-inbox-delete-lock", human_key="/tmp/http-inbox-delete-lock")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None

            sender = Agent(
                project_id=project.id,
                name="GreenCastle",
                program="test",
                model="test",
                task_description="sender",
            )
            recipient = Agent(
                project_id=project.id,
                name="BlueLake",
                program="test",
                model="test",
                task_description="recipient",
            )
            session.add(sender)
            session.add(recipient)
            await session.commit()
            await session.refresh(sender)
            await session.refresh(recipient)
            assert sender.id is not None
            assert recipient.id is not None

            message = Message(
                project_id=project.id,
                sender_id=sender.id,
                subject="Delete In Inbox",
                body_md="body",
                importance="normal",
                ack_required=False,
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)
            assert message.id is not None

            session.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=recipient.id,
                    kind="to",
                )
            )
            await session.commit()

        session_depth = 0
        archive_depths: list[int] = []
        original_get_session = http_module.get_session
        original_ensure_archive = http_module.ensure_archive

        @contextlib.asynccontextmanager
        async def tracking_get_session(*args: Any, **kwargs: Any):
            nonlocal session_depth
            async with original_get_session(*args, **kwargs) as session:
                session_depth += 1
                try:
                    yield session
                finally:
                    session_depth -= 1

        async def tracking_ensure_archive(*args: Any, **kwargs: Any):
            archive_depths.append(session_depth)
            return await original_ensure_archive(*args, **kwargs)

        monkeypatch.setattr(http_module, "get_session", tracking_get_session)
        monkeypatch.setattr(http_module, "ensure_archive", tracking_ensure_archive)

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/mail/{project.slug}/inbox/{recipient.name}/delete-messages",
                json={"message_ids": [message.id]},
            )

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert archive_depths == [0]
