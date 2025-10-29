import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_http_jwt_rbac_and_rate_limit(monkeypatch):
    # Configure bearer auth and RBAC
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
    monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
    # Enable rate limiting with small threshold
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "2")
    # Disable localhost auto-authentication to require credentials
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Without auth => 401
        r = await client.post(settings.http.path, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 401

        headers = {"Authorization": "Bearer token123"}

        # Reader can call read-only tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 200
        body = r.json()
        # Response is MCP JSON-RPC format with structuredContent
        assert body.get("result", {}).get("structuredContent", {}).get("status") == "ok"

        # Reader cannot call write tool (ensure_project requires writer privileges)
        r = await client.post(
            settings.http.path,
            headers=headers,
            json=_rpc(
                "tools/call",
                {"name": "ensure_project", "arguments": {"human_key": "/data/projects/http_rbac"}},
            ),
        )
        assert r.status_code == 403

        # Rate limit triggers on third tools call within window
        r1 = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code == 200
        r2 = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r2.status_code == 429

