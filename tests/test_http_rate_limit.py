"""HTTP Rate Limiting Tests.

Comprehensive tests for HTTP rate limiting mechanisms:
1. Rate limit enabled/disabled behavior
2. Token bucket burst capacity
3. Token bucket refill rate
4. Per-endpoint limits (tools vs resources)
5. Rate limit key derivation
6. 429 response format
7. Health endpoint bypass
8. Different limits for different endpoint types

Reference: mcp_agent_mail-9z5
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import SecurityAndRateLimitMiddleware, build_http_app


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


# =============================================================================
# Test: Rate Limit Enabled/Disabled
# =============================================================================


class TestRateLimitEnabledDisabled:
    """Test rate limit enable/disable configuration."""

    @pytest.mark.asyncio
    async def test_rate_limit_disabled_allows_unlimited_requests(self, isolated_env, monkeypatch):
        """With rate limiting disabled, unlimited requests are allowed."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Make many requests - all should succeed
            for _ in range(10):
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_limit_enabled_enforces_limits(self, isolated_env, monkeypatch):
        """With rate limiting enabled, requests beyond burst are blocked."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "2")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First 2 requests (burst) should succeed
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200

            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 200

            # Third request should be rate limited
            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 429


# =============================================================================
# Test: Token Bucket Burst Capacity
# =============================================================================


class TestTokenBucketBurst:
    """Test token bucket burst capacity behavior."""

    @pytest.mark.asyncio
    async def test_burst_allows_multiple_rapid_requests(self, isolated_env, monkeypatch):
        """Burst capacity allows multiple rapid requests."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "60")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "5")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # All 5 requests within burst should succeed
            success_count = 0
            for _ in range(5):
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                if response.status_code == 200:
                    success_count += 1

            assert success_count == 5

    @pytest.mark.asyncio
    async def test_burst_of_one_limits_immediately(self, isolated_env, monkeypatch):
        """With burst=1, second request is immediately rate limited."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200

            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 429

    @pytest.mark.asyncio
    async def test_burst_defaults_to_rpm_when_not_set(self, isolated_env, monkeypatch):
        """When burst is 0 or not set, it defaults to max(1, rpm)."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "3")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "0")  # Will default to 3
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Should allow 3 requests (burst defaults to rpm)
            success_count = 0
            for _ in range(4):
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                if response.status_code == 200:
                    success_count += 1

            assert success_count == 3


# =============================================================================
# Test: Per-Endpoint Rate Limits
# =============================================================================


class TestPerEndpointLimits:
    """Test different rate limits for tools vs resources."""

    @pytest.mark.asyncio
    async def test_tools_and_resources_have_separate_limits(self, isolated_env, monkeypatch):
        """Tools and resources have independent rate limit buckets."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_BURST", "2")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust tools limit
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            # Tools should be limited now
            r_tool = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r_tool.status_code == 429

            # But resources should still work (separate bucket)
            r_resource = await client.post(
                settings.http.path,
                json=_rpc("resources/list", {}),
            )
            assert r_resource.status_code == 200

    @pytest.mark.asyncio
    async def test_resources_have_higher_default_limit(self, isolated_env, monkeypatch):
        """Resources default to higher rate limit than tools."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        # Using different limits for tools (lower) vs resources (higher)
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "5")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_BURST", "5")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Resources should allow more requests
            success_count = 0
            for _ in range(5):
                response = await client.post(
                    settings.http.path,
                    json=_rpc("resources/list", {}),
                )
                if response.status_code == 200:
                    success_count += 1

            assert success_count == 5


# =============================================================================
# Test: 429 Response Format
# =============================================================================


class TestRateLimitResponse:
    """Test rate limit exceeded response format."""

    @pytest.mark.asyncio
    async def test_429_returns_json_response(self, isolated_env, monkeypatch):
        """Rate limit exceeded returns JSON with detail message."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First request succeeds
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            # Second request is rate limited
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 429
            data = response.json()
            assert "detail" in data
            assert data["detail"] == "Rate limit exceeded"

    @pytest.mark.asyncio
    async def test_429_content_type_is_json(self, isolated_env, monkeypatch):
        """Rate limit response has JSON content type."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 429
            assert "application/json" in response.headers.get("content-type", "")


# =============================================================================
# Test: Health Endpoint Bypass
# =============================================================================


class TestHealthEndpointBypass:
    """Test that health endpoints bypass rate limiting."""

    @pytest.mark.asyncio
    async def test_health_liveness_bypasses_rate_limit(self, isolated_env, monkeypatch):
        """Liveness endpoint is not rate limited."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Many health checks should all succeed
            for _ in range(10):
                response = await client.get("/health/liveness")
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_readiness_bypasses_rate_limit(self, isolated_env, monkeypatch):
        """Readiness endpoint is not rate limited."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Many readiness checks should all succeed (or fail for other reasons, not rate limit)
            for _ in range(10):
                response = await client.get("/health/readiness")
                assert response.status_code != 429


# =============================================================================
# Test: Rate Limit Helper Functions
# =============================================================================


class TestRateLimitHelpers:
    """Test internal rate limiting helper functions."""

    def test_rate_limits_for_tools(self):
        """_rate_limits_for returns correct values for tools."""
        settings = MagicMock()
        settings.http.rate_limit_tools_per_minute = 60
        settings.http.rate_limit_tools_burst = 10
        settings.http.rate_limit_enabled = True

        middleware = SecurityAndRateLimitMiddleware.__new__(SecurityAndRateLimitMiddleware)
        middleware.settings = settings

        rpm, burst = middleware._rate_limits_for("tools")
        assert rpm == 60
        assert burst == 10

    def test_rate_limits_for_resources(self):
        """_rate_limits_for returns correct values for resources."""
        settings = MagicMock()
        settings.http.rate_limit_resources_per_minute = 120
        settings.http.rate_limit_resources_burst = 20
        settings.http.rate_limit_enabled = True

        middleware = SecurityAndRateLimitMiddleware.__new__(SecurityAndRateLimitMiddleware)
        middleware.settings = settings

        rpm, burst = middleware._rate_limits_for("resources")
        assert rpm == 120
        assert burst == 20

    def test_rate_limits_burst_defaults_to_rpm(self):
        """When burst is 0, defaults to max(1, rpm)."""
        settings = MagicMock()
        settings.http.rate_limit_tools_per_minute = 30
        settings.http.rate_limit_tools_burst = 0  # Should default

        middleware = SecurityAndRateLimitMiddleware.__new__(SecurityAndRateLimitMiddleware)
        middleware.settings = settings

        rpm, burst = middleware._rate_limits_for("tools")
        assert rpm == 30
        assert burst == 30  # Defaults to rpm

    def test_rate_limits_burst_minimum_is_one(self):
        """Burst is at least 1 even when rpm would make it 0."""
        settings = MagicMock()
        # Note: The implementation uses `or 60` which means 0 becomes 60
        # So we test with a small rpm value to verify burst minimum
        settings.http.rate_limit_tools_per_minute = 1
        settings.http.rate_limit_tools_burst = 0

        middleware = SecurityAndRateLimitMiddleware.__new__(SecurityAndRateLimitMiddleware)
        middleware.settings = settings

        rpm, burst = middleware._rate_limits_for("tools")
        assert rpm == 1
        assert burst == 1  # max(1, rpm) = max(1, 1) = 1


# =============================================================================
# Test: Rate Limit Key Derivation
# =============================================================================


class TestRateLimitKeyDerivation:
    """Test rate limit key construction."""

    @pytest.mark.asyncio
    async def test_per_tool_rate_limiting(self, isolated_env, monkeypatch):
        """Different tools have separate rate limit buckets."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "2")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust health_check limit
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            # health_check should be limited
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 429

            # But a different tool should work (separate key per tool name)
            # Note: fetch_inbox may require registration, so this tests the key derivation concept
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "whois", "arguments": {"project_key": "/test", "agent_name": "Test"}}),
            )
            # Should not be 429 (different bucket), but may be 200 or other error
            assert r2.status_code != 429


# =============================================================================
# Test: CORS Preflight Bypass
# =============================================================================


class TestCORSPreflightBypass:
    """Test that CORS preflight bypasses rate limiting."""

    @pytest.mark.asyncio
    async def test_options_bypasses_rate_limit(self, isolated_env, monkeypatch):
        """OPTIONS requests bypass rate limiting."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "*")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Many OPTIONS requests should all succeed
            for _ in range(10):
                response = await client.options(
                    settings.http.path,
                    headers={
                        "Origin": "http://example.com",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert response.status_code in (200, 204)


# =============================================================================
# Test: Zero/Negative Rate Limit
# =============================================================================


class TestZeroRateLimit:
    """Test behavior with zero or negative rate limits."""

    @pytest.mark.asyncio
    async def test_zero_rpm_allows_all_requests(self, isolated_env, monkeypatch):
        """With rate_limit_per_minute=0, all requests are allowed."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "0")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # With rpm=0, rate limiting is effectively disabled for this endpoint type
            # The _consume_bucket returns True if per_minute <= 0
            for _ in range(5):
                response = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                assert response.status_code == 200
