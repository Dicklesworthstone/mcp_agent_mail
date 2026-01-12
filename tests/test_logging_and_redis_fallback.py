from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


@pytest.mark.asyncio
async def test_log_json_enabled_path(isolated_env, monkeypatch):
    # Enable JSON logging in settings to hit JSONRenderer branch
    monkeypatch.setenv("LOG_JSON_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Liveness should work; logging config path executed on app build
        r = await client.get("/health/liveness")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_redis_fallback(isolated_env, monkeypatch):
    # Force redis backend but make import fail so it falls back to memory
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    # Simulate import failure by shadowing importlib.import_module to raise for redis.asyncio
    import importlib

    def fake_import(name: str, *a, **k):
        if name == "redis.asyncio":
            raise ImportError("no redis")
        return real_import(name, *a, **k)

    real_import = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", fake_import)

    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/liveness")
        assert r.status_code == 200
