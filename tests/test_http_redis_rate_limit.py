from __future__ import annotations

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
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_rate_limit_redis_backend_path(isolated_env, monkeypatch):
    # Enable rate limiting with redis backend
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
    # Disable RBAC to avoid auth noise
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # Provide a fake redis.asyncio module
    class FakeRedis:
        @classmethod
        def from_url(cls, url: str):
            return cls()

        async def eval(self, script: str, numkeys: int, *args):
            # Always allow (return 1)
            return 1

    fake_pkg = cast(Any, ModuleType("redis.asyncio"))
    fake_pkg.Redis = FakeRedis
    sys.modules["redis.asyncio"] = fake_pkg

    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Call a resource twice; redis path should be exercised and allow both
        r1 = await client.post(settings.http.path, json=_rpc("resources/read", {"uri": "resource://projects"}))
        assert r1.status_code in (200, 429)
        r2 = await client.post(settings.http.path, json=_rpc("resources/read", {"uri": "resource://projects"}))
        assert r2.status_code in (200, 429)
