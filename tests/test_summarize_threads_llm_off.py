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
        # Create messages in two threads to trigger multi-thread mode
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Thread 1 msg",
                "body_md": "- TODO one",
                "thread_id": "T-1",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Thread 2 msg",
                "body_md": "- ACTION go",
                "thread_id": "T-2",
            },
        )

        # Use comma-separated thread_id for multi-thread mode
        res = await client.call_tool(
            "summarize_thread",
            {"project_key": "Backend", "thread_id": "T-1,T-2", "llm_mode": False},
        )
        data = res.data
        assert data.get("threads") and data.get("aggregate") is not None


