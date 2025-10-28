from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest
from fastmcp import Client, Context

from mcp_agent_mail.app import (
    ToolExecutionError,
    _enforce_capabilities,
    _iso,
    _parse_iso,
    _parse_json_safely,
    build_mcp_server,
)


def test_iso_and_parse_helpers():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert _iso(now).endswith("+00:00")
    assert _iso(now.isoformat()).endswith("+00:00")
    assert _iso("not-iso") == "not-iso"

    parsed = _parse_iso("2025-01-01T00:00:00Z")
    assert parsed is not None and parsed.year == 2025
    assert _parse_iso("bad-value") is None

    raw = '{"a": 1}'
    assert _parse_json_safely(raw) == {"a": 1}
    fenced = """```json\n{\n  \"x\": 2\n}\n```"""
    assert _parse_json_safely(fenced) == {"x": 2}
    noisy = "xxx {\n \"y\": 3\n} yyy"
    assert _parse_json_safely(noisy) == {"y": 3}


def test_enforce_capabilities_denied():
    # Minimal stand-in that matches the Context metadata surface
    class DummyCtx:
        def __init__(self):
            self.metadata = {"allowed_capabilities": ["read", "audit"]}

    # Call through and expect a ToolExecutionError with explanatory message
    with pytest.raises(ToolExecutionError) as exc:
        _enforce_capabilities(cast(Context, DummyCtx()), {"write"}, "send_message")
    assert "requires capabilities" in str(exc.value)


@pytest.mark.asyncio
async def test_tool_metrics_resource_populates_after_calls(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        # call a couple tools to increment metrics
        res = await client.call_tool("health_check", {})
        assert res.data["status"] == "ok"
        await client.call_tool("ensure_project", {"human_key": "/backend"})

        # tooling metrics resource
        metrics_blocks = await client.read_resource("resource://tooling/metrics")
        assert metrics_blocks and metrics_blocks[0].text
        # the text is JSON; ensure tools list contains health_check
        assert "health_check" in metrics_blocks[0].text


