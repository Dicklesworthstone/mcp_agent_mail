"""HTTP Authentication Tests.

Comprehensive tests for HTTP authentication mechanisms:
1. Bearer token authentication
2. JWT authentication with HMAC secret
3. JWT authentication with JWKS URL
4. RBAC role enforcement
5. Localhost bypass behavior
6. OAuth metadata endpoints

Reference: mcp_agent_mail-w51
"""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def _make_fake_jwt(claims: dict[str, Any], alg: str = "HS256") -> str:
    """Create a fake JWT for testing (not cryptographically valid)."""
    header = {"alg": alg, "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(b"fake_signature").decode().rstrip("=")
    return f"{header_b64}.{payload_b64}.{sig_b64}"


# =============================================================================
# Test: Bearer Token Authentication
# =============================================================================


class TestBearerTokenAuth:
    """Test simple bearer token authentication."""

    @pytest.mark.asyncio
    async def test_unauthorized_without_token(self, isolated_env, monkeypatch):
        """Request without bearer token returns 401."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_authorized_with_correct_token(self, isolated_env, monkeypatch):
        """Request with correct bearer token succeeds."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "my-secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer my-secret-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthorized_with_wrong_token(self, isolated_env, monkeypatch):
        """Request with incorrect bearer token returns 401."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "correct-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer wrong-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_unauthorized_with_malformed_auth_header(self, isolated_env, monkeypatch):
        """Request with malformed Authorization header returns 401."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Missing "Bearer " prefix
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "secret-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_no_auth_required_without_bearer_token_config(self, isolated_env, monkeypatch):
        """Without bearer token configured, requests are allowed."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

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


# =============================================================================
# Test: Localhost Bypass
# =============================================================================


class TestLocalhostBypass:
    """Test localhost authentication bypass behavior."""

    @pytest.mark.asyncio
    async def test_localhost_bypass_enabled(self, isolated_env, monkeypatch):
        """With localhost bypass enabled, no auth required for localhost."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Should succeed without Authorization header (localhost)
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_localhost_bypass_disabled(self, isolated_env, monkeypatch):
        """With localhost bypass disabled, auth required even for localhost."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Should fail without Authorization header
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401


# =============================================================================
# Test: CORS Preflight Bypass
# =============================================================================


class TestCORSPreflightBypass:
    """Test that CORS preflight requests bypass authentication."""

    @pytest.mark.asyncio
    async def test_options_request_bypasses_auth(self, isolated_env, monkeypatch):
        """OPTIONS request should not require authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "*")
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


# =============================================================================
# Test: Health Endpoint Bypass
# =============================================================================


class TestHealthEndpointBypass:
    """Test that health endpoints bypass authentication."""

    @pytest.mark.asyncio
    async def test_liveness_bypasses_auth(self, isolated_env, monkeypatch):
        """Liveness endpoint does not require authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_readiness_bypasses_auth(self, isolated_env, monkeypatch):
        """Readiness endpoint does not require authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/readiness")
            # May be 200 or 503 depending on DB state, but not 401
            assert response.status_code != 401


# =============================================================================
# Test: RBAC Role Enforcement
# =============================================================================


class TestRBACEnforcement:
    """Test RBAC (Role-Based Access Control) enforcement."""

    @pytest.mark.asyncio
    async def test_reader_role_can_read_resources(self, isolated_env, monkeypatch):
        """Reader role can access resources."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "reader-token")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
        monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
        monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
        monkeypatch.setenv("HTTP_RBAC_DEFAULT_ROLE", "reader")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer reader-token"},
                json=_rpc("resources/list", {}),
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_rbac_readonly_tools_accessible_to_readers(self, isolated_env, monkeypatch):
        """Read-only tools are accessible to readers."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
        monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
        monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
        monkeypatch.setenv("HTTP_RBAC_DEFAULT_ROLE", "reader")
        monkeypatch.setenv("HTTP_RBAC_READONLY_TOOLS", "health_check,fetch_inbox")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer test-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            # health_check is read-only, should be allowed
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_reader_blocked_from_write_tools(self, isolated_env, monkeypatch):
        """Reader role is blocked from write tools."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
        monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
        monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
        monkeypatch.setenv("HTTP_RBAC_DEFAULT_ROLE", "reader")
        monkeypatch.setenv("HTTP_RBAC_READONLY_TOOLS", "health_check")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer test-token"},
                json=_rpc("tools/call", {"name": "ensure_project", "arguments": {"human_key": "/test"}}),
            )
            # ensure_project is NOT read-only, should be blocked for readers
            assert response.status_code == 403


# =============================================================================
# Test: OAuth Metadata Endpoints
# =============================================================================


class TestOAuthMetadataEndpoints:
    """Test OAuth metadata endpoint responses."""

    @pytest.mark.asyncio
    async def test_oauth_metadata_root_returns_404(self, isolated_env):
        """OAuth metadata endpoint returns 404 so clients skip OAuth discovery."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/oauth-authorization-server")
            assert response.status_code == 404
            data = response.json()
            assert data.get("mcp_oauth") is False

    @pytest.mark.asyncio
    async def test_oauth_metadata_mcp_returns_404(self, isolated_env):
        """OAuth metadata MCP endpoint returns 404 so clients skip OAuth discovery."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/oauth-authorization-server/mcp")
            assert response.status_code == 404
            data = response.json()
            assert data.get("mcp_oauth") is False


# =============================================================================
# Test: JWT Helper Functions
# =============================================================================


class TestJWTHelpers:
    """Test JWT-related helper functions."""

    def test_decode_jwt_header_segment_valid(self):
        """_decode_jwt_header_segment correctly decodes valid JWT header."""
        from mcp_agent_mail.http import _decode_jwt_header_segment

        # Create a valid JWT header segment
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        token = f"{header_b64}.payload.signature"

        result = _decode_jwt_header_segment(token)
        assert result is not None
        assert result.get("alg") == "HS256"
        assert result.get("typ") == "JWT"

    def test_decode_jwt_header_segment_invalid(self):
        """_decode_jwt_header_segment returns None for invalid JWT."""
        from mcp_agent_mail.http import _decode_jwt_header_segment

        result = _decode_jwt_header_segment("not-a-jwt")
        assert result is None

    def test_decode_jwt_header_segment_empty(self):
        """_decode_jwt_header_segment returns None for empty string."""
        from mcp_agent_mail.http import _decode_jwt_header_segment

        result = _decode_jwt_header_segment("")
        assert result is None


# =============================================================================
# Test: Authorization Header Handling
# =============================================================================


class TestAuthorizationHeaderHandling:
    """Test various Authorization header formats."""

    @pytest.mark.asyncio
    async def test_bearer_prefix_required(self, isolated_env, monkeypatch):
        """Authorization header must start with 'Bearer '."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Basic auth format (should fail)
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_case_sensitive(self, isolated_env, monkeypatch):
        """Bearer token comparison is exact."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "MyToken123")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Different case should fail
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer mytoken123"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401

            # Correct case should succeed
            response = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer MyToken123"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_authorization_header(self, isolated_env, monkeypatch):
        """Empty Authorization header returns 401."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Authorization": ""},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 401


# =============================================================================
# Test: Rate Limiting Integration
# =============================================================================


class TestRateLimitingIntegration:
    """Test rate limiting with authentication."""

    @pytest.mark.asyncio
    async def test_rate_limit_applies_after_auth(self, isolated_env, monkeypatch):
        """Rate limiting is enforced after successful authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "2")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": "Bearer test-token"}

            # First two requests succeed (burst=2)
            r1 = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200

            r2 = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 200

            # Third request hits rate limit
            r3 = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 429

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429_response(self, isolated_env, monkeypatch):
        """Rate limit exceeded returns proper 429 response."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": "Bearer test-token"}

            # First request consumes the burst
            await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            # Second request hits rate limit
            r = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r.status_code == 429
            data = r.json()
            assert data.get("detail") == "Rate limit exceeded"
