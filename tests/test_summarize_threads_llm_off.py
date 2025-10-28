from __future__ import annotations

import contextlib

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_summarize_threads_without_llm_path(isolated_env, monkeypatch):
    # Ensure LLM disabled to exercise non-LLM branch
    monkeypatch.setenv("LLM_ENABLED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"},
        )
        # Create thread messages
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "T1",
                "body_md": "- TODO one",
                "thread_id": "T-1",
            },
        )
        _ = m1.data
        m2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "T2",
                "body_md": "- ACTION go",
                "thread_id": "T-1",
            },
        )
        _ = m2.data

        res = await client.call_tool(
            "summarize_threads",
            {"project_key": "Backend", "thread_ids": ["T-1"], "llm_mode": False},
        )
        data = res.data
        assert data.get("threads") and data.get("aggregate") is not None


