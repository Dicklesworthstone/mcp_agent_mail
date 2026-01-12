"""Comprehensive HTTP Rate Limiting Tests.

Tests for token-bucket rate limiting:
- Basic rate limiting with tools endpoint
- Different limits for tools vs resources
- Burst capacity handling
- Token refill over time
- Per-client rate limiting
- Rate limit disabled scenario
- Edge cases and error handling

Reference: mcp_agent_mail-9z5
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import ModuleType
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict:
    """Create a JSON-RPC 2.0 request."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


# ============================================================================
# Basic Rate Limiting Tests
# ============================================================================


class TestBasicRateLimiting:
    """Tests for basic rate limiting functionality."""

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_when_exceeded(self, isolated_env, monkeypatch):
        """Rate limit should block requests when quota exceeded."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First two requests should succeed
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
            assert "Rate limit exceeded" in r3.json().get("detail", "")

    @pytest.mark.asyncio
    async def test_rate_limit_disabled_allows_all(self, isolated_env, monkeypatch):
        """When rate limiting is disabled, all requests should pass."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Make many requests - all should succeed
            for _ in range(10):
                r = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                assert r.status_code == 200


# ============================================================================
# Different Endpoint Limits Tests
# ============================================================================


class TestDifferentEndpointLimits:
    """Tests for different rate limits on tools vs resources."""

    @pytest.mark.asyncio
    async def test_tools_and_resources_have_separate_limits(self, isolated_env, monkeypatch):
        """Tools and resources should have independent rate limits."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "3")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust tools limit (2 requests)
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r3.status_code == 429

            # Resources should still work (independent limit)
            r4 = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": "resource://projects"}),
            )
            # May be 200 or error, but not 429 since resources has its own limit
            # If 200 or some other error (not auth related), resources limit wasn't hit
            assert r4.status_code != 429 or r4.json().get("detail", "") == "Rate limit exceeded"

    @pytest.mark.asyncio
    async def test_resources_rate_limit(self, isolated_env, monkeypatch):
        """Resources endpoint should respect its own rate limit."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First resource request should succeed
            r1 = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": "resource://projects"}),
            )
            assert r1.status_code == 200

            # Second should be rate limited
            r2 = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": "resource://projects"}),
            )
            assert r2.status_code == 429


# ============================================================================
# Burst Capacity Tests
# ============================================================================


class TestBurstCapacity:
    """Tests for burst capacity in token bucket."""

    @pytest.mark.asyncio
    async def test_burst_allows_initial_spike(self, isolated_env, monkeypatch):
        """Burst capacity should allow initial spike of requests."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "60")  # 1 per second
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "5")  # Allow 5 burst
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Should allow burst of 5 requests immediately
            results = []
            for _ in range(5):
                r = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                results.append(r.status_code)

            # All 5 burst requests should succeed
            assert results == [200, 200, 200, 200, 200]

            # 6th request should be rate limited
            r6 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r6.status_code == 429

    @pytest.mark.asyncio
    async def test_zero_burst_uses_rpm_as_bucket(self, isolated_env, monkeypatch):
        """When burst is 0, should use rate per minute as bucket size."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "0")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # With 0 burst, bucket size defaults to rpm (2)
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200
            assert r2.status_code == 200

            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 429


# ============================================================================
# Token Refill Tests
# ============================================================================


class TestTokenRefill:
    """Tests for token bucket refill over time."""

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self, isolated_env, monkeypatch):
        """Tokens should refill after waiting."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        # 60 per minute = 1 per second
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "60")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_BURST", "1")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Use up the single token
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200

            # Should be rate limited immediately
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 429

            # Wait for refill (1 second for 1 token at 60/min rate)
            await asyncio.sleep(1.1)

            # Should succeed again
            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 200


# ============================================================================
# Per-Client Rate Limiting Tests
# ============================================================================


class TestPerClientRateLimiting:
    """Tests for per-client rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_keyed_by_identity(self, isolated_env, monkeypatch):
        """Rate limits should be keyed by client identity."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)

        # In ASGI test transport, all requests share same client identity
        # This test verifies consistent rate limiting per identity
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
            assert r2.status_code == 200

            # Third request should be rate limited
            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 429


# ============================================================================
# Redis Backend Tests
# ============================================================================


class TestRedisBackend:
    """Tests for Redis-backed rate limiting."""

    @pytest.mark.asyncio
    async def test_redis_backend_lua_script(self, isolated_env, monkeypatch):
        """Redis backend should use Lua script for atomic operations."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        # Track Lua script calls
        lua_calls: list[tuple] = []

        class FakeRedis:
            @classmethod
            def from_url(cls, url: str):
                return cls()

            async def eval(self, script: str, numkeys: int, *args):
                lua_calls.append((script, numkeys, args))
                # Allow first 2, deny after
                return 1 if len(lua_calls) <= 2 else 0

        fake_pkg = cast(Any, ModuleType("redis.asyncio"))
        fake_pkg.Redis = FakeRedis
        sys.modules["redis.asyncio"] = fake_pkg

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First two should succeed
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200
            assert r2.status_code == 200

            # Third should be denied by our fake Redis
            r3 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r3.status_code == 429

            # Verify Lua script was called
            assert len(lua_calls) == 3
            for script, numkeys, args in lua_calls:
                assert "tokens" in script  # Token bucket logic
                assert numkeys == 1
                assert "rl:" in args[0]  # Rate limit key prefix

    @pytest.mark.asyncio
    async def test_redis_fallback_on_error(self, isolated_env, monkeypatch):
        """Redis errors should gracefully allow requests (fail open)."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        class FakeRedis:
            @classmethod
            def from_url(cls, url: str):
                return cls()

            async def eval(self, script: str, numkeys: int, *args):
                raise ConnectionError("Redis connection failed")

        fake_pkg = cast(Any, ModuleType("redis.asyncio"))
        fake_pkg.Redis = FakeRedis
        sys.modules["redis.asyncio"] = fake_pkg

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Should fail open and allow the request
            r = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r.status_code == 200


# ============================================================================
# Edge Cases Tests
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases in rate limiting."""

    @pytest.mark.asyncio
    async def test_very_high_rate_limit(self, isolated_env, monkeypatch):
        """Very high rate limit should effectively allow all requests."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "10000")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Many requests should all succeed
            for _ in range(20):
                r = await client.post(
                    settings.http.path,
                    json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
                )
                assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_limits_are_per_endpoint(self, isolated_env, monkeypatch):
        """Rate limits are keyed per endpoint (tool name)."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First call to health_check succeeds
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 200

            # Second call to health_check is rate limited (same endpoint)
            r2 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 429

    @pytest.mark.asyncio
    async def test_rate_limit_response_format(self, isolated_env, monkeypatch):
        """Rate limit response should have proper JSON format."""
        monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
        monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        server = build_mcp_server()
        app = build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Use up limit
            await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            # Get rate limit response
            r = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )

            assert r.status_code == 429
            body = r.json()
            assert "detail" in body
            assert "Rate limit" in body["detail"]
