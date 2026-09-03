"""Regression tests for issue #250: stateful MCP sessions over HTTP.

The '/mcp' mount must be a STATEFUL streamable-HTTP endpoint that issues an
``Mcp-Session-Id`` header on initialize, so session-bound agent authentication
(#148) can persist across separate HTTP tool calls. The '/api' mount must stay
STATELESS so handshake-skipping one-shot clients (e.g. ntm's HTTP client) keep
working.
"""

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "issue-250-regression", "version": "0.0.1"},
        },
    }


def _tools_call_payload(name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


@pytest.fixture()
def http_app(monkeypatch):
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token250")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    # The default deployment shape: base '/api' (stateless), '/mcp' compat
    # alias stateful. (The repo-wide conftest pins HTTP_PATH=/mcp/, which is
    # the explicit-legacy-base case where '/mcp' stays stateless.)
    monkeypatch.setenv("HTTP_PATH", "/api/")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    return build_http_app(settings, server)


AUTH = {"Authorization": "Bearer token250"}


@pytest.mark.asyncio
async def test_mcp_mount_issues_session_id_and_session_persists(http_app):
    """'/mcp' initialize returns Mcp-Session-Id, and follow-up calls reusing
    that ID succeed — the transport-level prerequisite for #148 session-bound
    auth over HTTP."""
    transport = ASGITransport(app=http_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/mcp/", headers=AUTH, json=_initialize_payload())
        assert r.status_code == 200, r.text
        session_id = r.headers.get("mcp-session-id")
        assert session_id, (
            "stateful '/mcp' mount must return an Mcp-Session-Id header on "
            "initialize (issue #250: without it, session-bound auth can never "
            "persist across HTTP tool calls)"
        )

        session_headers = {**AUTH, "mcp-session-id": session_id}

        # Complete the handshake within the same session.
        r = await client.post(
            "/mcp/",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert r.status_code in (200, 202), r.text

        # A protected follow-up call in the same session must be accepted by
        # the transport (i.e. the session ID is recognized, not orphaned).
        r = await client.post(
            "/mcp/",
            headers=session_headers,
            json=_tools_call_payload("health_check"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("result", {}).get("structuredContent", {}).get("status") == "ok"


@pytest.mark.asyncio
async def test_api_mount_stays_stateless_for_one_shot_clients(http_app):
    """'/api' must keep accepting handshake-free one-shot calls (ntm-style
    clients) and must not require or bind a session."""
    transport = ASGITransport(app=http_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No initialize, no session header — straight to a tool call.
        r = await client.post(
            "/api/",
            headers=AUTH,
            json=_tools_call_payload("health_check"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("result", {}).get("structuredContent", {}).get("status") == "ok"


@pytest.mark.asyncio
async def test_explicit_mcp_base_keeps_legacy_stateless_semantics(monkeypatch):
    """An operator who explicitly configures HTTP_PATH=/mcp/ has promised
    existing clients handshake-free semantics there — the #250 change must not
    alter the configured base's behavior."""
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token250")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/mcp/",
            headers=AUTH,
            json=_tools_call_payload("health_check"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("result", {}).get("structuredContent", {}).get("status") == "ok"


@pytest.mark.asyncio
async def test_mcp_and_api_mounts_are_distinct_apps(http_app):
    """Mount table sanity: '/mcp' and '/api' resolve to different MCP sub-apps
    (stateful vs stateless), not the same app under two names."""
    mounted = {}
    for route in http_app.routes:
        path = getattr(route, "path", None)
        app = getattr(route, "app", None)
        if path in ("/mcp", "/api") and app is not None:
            mounted[path] = app
    assert set(mounted) == {"/mcp", "/api"}
    assert mounted["/mcp"] is not mounted["/api"], (
        "'/mcp' (stateful) and '/api' (stateless) must be distinct MCP apps"
    )
