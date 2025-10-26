from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from authlib.jose import JsonWebKey, jwt
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_http_bearer_and_cors_preflight(isolated_env, monkeypatch):
    # Enable Bearer and CORS
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
    monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
    monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
    # Disable localhost auto-authentication to properly test bearer auth
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Preflight OPTIONS
        r0 = await client.options(settings.http.path, headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        })
        assert r0.status_code in (200, 204)
        # No bearer -> 401
        r1 = await client.post(settings.http.path, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code == 401
        # With bearer
        r2 = await client.post(
            settings.http.path,
            headers={"Authorization": "Bearer token123", "Origin": "http://example.com"},
            json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
        )
        assert r2.status_code == 200
        # CORS header present on response
        assert r2.headers.get("access-control-allow-origin") in ("*", "http://example.com")


@pytest.mark.asyncio
async def test_http_jwks_validation_and_resource_rate_limit(isolated_env, monkeypatch):
    # Configure JWT with JWKS and strict resource rate limit
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "RS256")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
    monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "1")
    monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "10")
    # Provide a JWKS URL (dummy) and monkeypatch HTTP call
    monkeypatch.setenv("HTTP_JWT_JWKS_URL", "https://jwks.local/keys")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # Generate RSA key + JWKS using Authlib utilities
    private_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    private_jwk["kid"] = "abc"
    public_jwk = JsonWebKey.import_key(private_jwk).as_dict(is_private=False)
    jwks_payload = {"keys": [public_jwk]}

    async def fake_get(self, url: str):  # type: ignore[override]
        class _Resp:
            status_code = 200
            def json(self) -> dict[str, Any]:
                return jwks_payload
        return _Resp()

    # Build token with RS256
    token = (
        jwt.encode(
            {"alg": "RS256", "kid": "abc"},
            {"sub": "u1", settings.http.jwt_role_claim: "reader"},
            private_jwk,
        ).decode("utf-8")
    )

    server = build_mcp_server()
    app = build_http_app(settings, server)

    # Patch httpx.AsyncClient.get used in JWKS fetch path
    import httpx  # type: ignore
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        # Reader can call read-only tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 200
        # Resource rate limit 1 rpm -> second call 429
        r1 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://projects"}))
        assert r1.status_code in (200, 429)
        r2 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://projects"}))
        assert r2.status_code == 429


@pytest.mark.asyncio
async def test_http_path_mount_trailing_and_no_slash(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        base = settings.http.path.rstrip("/")
        r1 = await client.post(base, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code in (200, 401, 403)
        r2 = await client.post(base + "/", json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r2.status_code in (200, 401, 403)


@pytest.mark.asyncio
async def test_http_readiness_endpoint(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        assert r.status_code in (200, 503)


@pytest.mark.asyncio
async def test_http_lock_status_endpoint(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)

    storage_root = Path(settings.storage.root).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".archive.lock"
    lock_path.touch()
    metadata_path = storage_root / ".archive.lock.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": time.time() - 400}), encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/locks")
        assert resp.status_code == 200
        payload = resp.json()
        locks = payload.get("locks", [])
        assert any(item.get("path") == str(lock_path) for item in locks)
        entry = next(item for item in locks if item.get("path") == str(lock_path))
        assert entry.get("metadata", {}).get("pid") == 999_999
        assert entry.get("stale_suspected") is True

