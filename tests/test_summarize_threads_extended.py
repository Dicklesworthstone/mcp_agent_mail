from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_summarize_threads_non_llm_mode_and_limit(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"},
        )
        # Create messages under two threads
        for tid in ("T1", "T2"):
            for i in range(3):
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": "Backend",
                        "sender_name": "BlueLake",
                        "to": ["BlueLake"],
                        "subject": f"{tid}-{i}",
                        "body_md": f"body {tid} {i}",
                        "thread_id": tid,
                    },
                )
        res = await client.call_tool(
            "summarize_threads",
            {"project_key": "Backend", "thread_ids": ["T1", "T2"], "llm_mode": False, "per_thread_limit": 2},
        )
        data = res.data
        assert isinstance(data.get("threads"), list)
        # Expect summaries for both thread ids
        tids = {t.get("thread_id") for t in data.get("threads")}
        assert {"T1", "T2"}.issubset(tids)


