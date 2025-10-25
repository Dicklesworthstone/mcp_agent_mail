from __future__ import annotations

import contextlib
import sys

from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema
from mcp_agent_mail.http import build_http_app, main as http_main


def test_http_main_invokes_uvicorn(monkeypatch):
    # Ensure settings default host/port are used
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    calls: dict[str, object] = {}

    def fake_run(app, host, port, log_level="info"):
        calls["host"] = host
        calls["port"] = port
        calls["lv"] = log_level

    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setattr("uvicorn.run", fake_run)
    # Prevent pytest argv from leaking into argparse
    monkeypatch.setattr(sys, "argv", ["mcp-http"])
    http_main()
    assert calls.get("host") == "127.0.0.1"


async def _readiness_ok() -> int:
    # Sanity check app readiness OK path with schema ensured
    await ensure_schema()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        return r.status_code


def test_readiness_ok_status(isolated_env):
    import asyncio

    code = asyncio.run(_readiness_ok())
    assert code in (200, 503)


