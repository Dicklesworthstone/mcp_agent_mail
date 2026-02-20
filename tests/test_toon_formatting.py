from __future__ import annotations

import json
import subprocess

import pytest
from fastmcp import Client

from mcp_agent_mail import app as app_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache


def _fake_completed(stdout: str, stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["tru", "--encode"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_tool_format_toon_envelope(isolated_env, monkeypatch):
    monkeypatch.setenv("TOON_STATS", "true")
    monkeypatch.setattr(app_module, "_looks_like_toon_rust_encoder", lambda _exe: True)
    clear_settings_cache()

    def _fake_run(payload: str, settings):
        _ = payload, settings
        return _fake_completed(
            stdout="project:\n  ok\n",
            stderr="Token estimates: ~10 (JSON) -> ~5 (TOON)\nSaved ~5 tokens (-50.0%)\n",
        )

    monkeypatch.setattr(app_module, "_run_toon_encode", _fake_run)
    server = build_mcp_server()

    async with Client(server) as client:
        result = await client.call_tool("health_check", {"format": "toon"})
        payload = result.data
        assert isinstance(payload, dict)
        assert payload.get("format") == "toon"
        assert isinstance(payload.get("data"), str)
        meta = payload.get("meta") or {}
        assert meta.get("encoder") == "tru"
        stats = meta.get("toon_stats") or {}
        assert stats.get("json_tokens") == 10
        assert stats.get("toon_tokens") == 5


@pytest.mark.asyncio
async def test_resource_format_toon_envelope(isolated_env, monkeypatch):
    monkeypatch.setattr(app_module, "_looks_like_toon_rust_encoder", lambda _exe: True)
    clear_settings_cache()

    def _fake_run(payload: str, settings):
        _ = payload, settings
        return _fake_completed(stdout="projects:\n  - slug: backend\n")

    monkeypatch.setattr(app_module, "_run_toon_encode", _fake_run)
    server = build_mcp_server()

    async with Client(server) as client:
        blocks = await client.read_resource("resource://projects?format=toon")
        assert blocks and blocks[0].text
        payload = json.loads(blocks[0].text)
        assert payload.get("format") == "toon"
        assert isinstance(payload.get("data"), str)


@pytest.mark.asyncio
async def test_resource_format_query_param_fastmcp(isolated_env, monkeypatch):
    monkeypatch.setattr(app_module, "_looks_like_toon_rust_encoder", lambda _exe: True)
    clear_settings_cache()

    def _fake_run(payload: str, settings):
        _ = payload, settings
        return _fake_completed(stdout="environment:\n  local\n")

    monkeypatch.setattr(app_module, "_run_toon_encode", _fake_run)
    server = build_mcp_server()

    async with Client(server) as client:
        blocks = await client.read_resource("resource://config/environment?format=toon")
        assert blocks and blocks[0].text
        payload = json.loads(blocks[0].text)
        assert payload.get("format") == "toon"
        assert isinstance(payload.get("data"), str)


@pytest.mark.asyncio
async def test_toon_fallback_on_encoder_error(isolated_env, monkeypatch):
    clear_settings_cache()

    def _fake_run(payload: str, settings):
        _ = payload, settings
        return _fake_completed(stdout="", stderr="boom", returncode=1)

    monkeypatch.setattr(app_module, "_run_toon_encode", _fake_run)
    server = build_mcp_server()

    async with Client(server) as client:
        result = await client.call_tool("health_check", {"format": "toon"})
        payload = result.data
        assert payload.get("format") == "json"
        meta = payload.get("meta") or {}
        assert "toon_error" in meta
