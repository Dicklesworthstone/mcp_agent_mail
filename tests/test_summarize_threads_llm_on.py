from __future__ import annotations

import contextlib

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


class _StubOut:
    def __init__(self, text: str):
        self.content = text
        self.model = "m"
        self.provider = "p"


@pytest.mark.asyncio
async def test_summarize_threads_llm_refinement(isolated_env, monkeypatch):
    # Force LLM enabled
    from mcp_agent_mail import config as _config

    monkeypatch.setenv("LLM_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    # Monkeypatch LLM call to return JSON content the app will parse
    from mcp_agent_mail import app as app_mod

    async def _fake_complete(*_a, **_k):
        return _StubOut(
            '{"threads": [{"thread_id": "T-1", "key_points": ["refined"], "actions": ["do"]}, {"thread_id": "T-2", "key_points": ["also refined"], "actions": ["act"]}], "aggregate": {"top_mentions": [], "key_points": ["K"], "action_items": ["A"]}}'
        )

    monkeypatch.setattr(app_mod, "complete_system_user", _fake_complete)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"},
        )
        # Seed two threads to trigger multi-thread mode
        for tid in ("T-1", "T-2"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": f"Msg in {tid}",
                    "body_md": "body",
                    "thread_id": tid,
                },
            )

        # Use comma-separated thread_id for multi-thread mode
        res = await client.call_tool(
            "summarize_thread",
            {"project_key": "Backend", "thread_id": "T-1,T-2", "llm_mode": True, "per_thread_limit": 5},
        )
        payload = res.data
        assert payload.get("threads")
        # Ensure LLM-refined aggregate keys present
        agg = payload.get("aggregate") or {}
        assert agg.get("action_items") == ["A"]

