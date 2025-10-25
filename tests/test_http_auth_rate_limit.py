import contextlib

import pytest
from authlib.jose import jwt
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_http_jwt_rbac_and_rate_limit(monkeypatch):
    # Configure JWT and RBAC
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_SECRET", "secret")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    # Reader role only
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
    monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
    # Enable rate limiting with small threshold
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "1")
    # Disable localhost auto-authentication to properly test RBAC
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

        # Build JWT for a reader
        claims = {"sub": "user-1", settings.http.jwt_role_claim: "reader"}
        token = jwt.encode({"alg": "HS256"}, claims, settings.http.jwt_secret).decode("utf-8")
        headers = {"Authorization": f"Bearer {token}"}

        # Reader can call read-only tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 200
        body = r.json()
        # Response is MCP JSON-RPC format with structuredContent
        assert body.get("result", {}).get("structuredContent", {}).get("status") == "ok"

        # Reader cannot call write tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "send_message", "arguments": {"project_key": "Backend", "sender_name": "A", "to": ["B"], "subject": "x", "body_md": "y"}}))
        assert r.status_code == 403

        # Rate limit triggers on second tools call within window
        r1 = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code in (200, 429)
        r2 = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r2.status_code == 429


