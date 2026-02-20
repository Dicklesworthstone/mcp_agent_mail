from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_request_logging_middleware_and_liveness(isolated_env, monkeypatch):
    monkeypatch.setenv("HTTP_REQUEST_LOG_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/liveness")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_readiness_error_path_returns_503(isolated_env, monkeypatch):
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()

    # Force readiness failure
    import mcp_agent_mail.http as http_mod

    async def fail_readiness() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(http_mod, "readiness_check", fail_readiness)
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        assert r.status_code == 503


@pytest.mark.asyncio
async def test_rbac_denies_when_tool_name_missing(isolated_env, monkeypatch):
    # Enable RBAC but no JWT auth -> unauthenticated tool calls should be denied.
    # The exact status code varies by platform/transport (401 or 403) due to
    # differences in how ASGITransport handles client addresses.
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    # Disable localhost auto-authentication to properly test RBAC
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(settings.http.path, json=_rpc("tools/call", {"arguments": {}}))
        # Accept either 401 (Unauthorized) or 403 (Forbidden) - both indicate access denied
        assert r.status_code in {401, 403}, f"Expected 401 or 403, got {r.status_code}"
