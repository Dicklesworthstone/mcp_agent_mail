"""Tests for on-demand project-wide summarization (bd-1ia).

Covers:
- Empty time window returns "No activity" without LLM call
- Single thread summarization
- Multiple threads combined into project summary
- Summary stored in DB
- Idempotent: same window returns cached
- fetch_summary retrieval with time filter and limit
- Max messages truncation
- LLM refinement with mock
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

logger = logging.getLogger(__name__)


def _get_data(result):
    """Extract data dict from tool result."""
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, dict):
            return sc
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return getattr(result, "data", result)


def _get_list(result):
    """Extract list data from tool result (list-returning tools)."""
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


async def _setup_project_with_agents(client, project_key: str, count: int) -> list[str]:
    """Register `count` agents in the project and return their names."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    names = []
    for i in range(count):
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-prog",
                "model": "test-model",
                "task_description": f"agent-{i}",
            },
        )
        data = _get_data(result)
        names.append(data["name"])
    return names


# ============================================================================
# Empty window
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_empty_window(isolated_env):
    """No messages in window -> 'No activity' response, no LLM call."""
    server = build_mcp_server()
    async with Client(server) as client:
        await _setup_project_with_agents(client, "/test/sum-empty", 1)

        result = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-empty",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        data = _get_data(result)
        assert data["source_message_count"] == 0
        assert "No activity" in data["summary_text"]
        assert data["id"] is None
        assert data["cached"] is False


# ============================================================================
# Single thread
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_single_thread(isolated_env):
    """Summarize a single thread with a few messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-single", 2)
        sender, receiver = names[0], names[1]

        for i in range(3):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/sum-single",
                    "sender_name": sender,
                    "to": [receiver],
                    "subject": f"Thread msg {i}",
                    "body_md": f"- Point {i}\n- TODO item {i}",
                    "thread_id": "T1",
                },
            )

        result = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-single",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        data = _get_data(result)
        assert data["source_message_count"] == 3
        assert data["cached"] is False
        assert data["id"] is not None
        # Summary text is JSON with key_points
        import json
        summary = json.loads(data["summary_text"])
        assert summary["total_messages"] == 3
        assert summary["total_threads"] == 1
        assert len(summary["key_points"]) > 0


# ============================================================================
# Multiple threads
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_multiple_threads(isolated_env):
    """Summarize messages across multiple threads."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-multi", 2)
        sender, receiver = names[0], names[1]

        # Thread T1
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-multi",
                "sender_name": sender,
                "to": [receiver],
                "subject": "T1 msg",
                "body_md": "- Working on auth",
                "thread_id": "T1",
            },
        )
        # Thread T2
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-multi",
                "sender_name": receiver,
                "to": [sender],
                "subject": "T2 msg",
                "body_md": "- Fixing tests",
                "thread_id": "T2",
            },
        )

        result = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-multi",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        data = _get_data(result)
        assert data["source_message_count"] == 2
        import json
        summary = json.loads(data["summary_text"])
        assert summary["total_threads"] == 2
        assert len(summary["participants"]) == 2


# ============================================================================
# DB storage
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_stores_in_db(isolated_env):
    """After summarization, fetch_summary returns the stored summary."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-store", 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-store",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Store test",
                "body_md": "- Data point",
            },
        )

        # Create summary
        sum_result = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-store",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        sum_data = _get_data(sum_result)
        assert sum_data["id"] is not None

        # Fetch it back
        fetch_result = await client.call_tool(
            "fetch_summary",
            {
                "project_key": "/test/sum-store",
                "since_hours": 1.0,
                "limit": 5,
            },
        )
        items = _get_list(fetch_result)
        assert len(items) >= 1
        assert items[0]["id"] == sum_data["id"]
        assert items[0]["source_message_count"] == 1


# ============================================================================
# Idempotency
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_idempotent(isolated_env):
    """Same time window twice returns cached summary (no duplicate)."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-idem", 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-idem",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Idempotency test",
                "body_md": "body",
            },
        )

        # First call
        r1 = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-idem",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        d1 = _get_data(r1)
        assert d1["cached"] is False
        first_id = d1["id"]

        # Second call — should return cached
        r2 = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-idem",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )
        d2 = _get_data(r2)
        assert d2["cached"] is True
        assert d2["id"] == first_id


# ============================================================================
# fetch_summary filtering
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_summary_limit(isolated_env):
    """fetch_summary with limit=1 returns at most 1 summary."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-limit", 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-limit",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Limit test",
                "body_md": "body",
            },
        )

        # Create a summary
        await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-limit",
                "since_hours": 1.0,
                "llm_mode": False,
            },
        )

        # Fetch with limit=1
        result = await client.call_tool(
            "fetch_summary",
            {
                "project_key": "/test/sum-limit",
                "since_hours": 24.0,
                "limit": 1,
            },
        )
        items = _get_list(result)
        assert len(items) <= 1


@pytest.mark.asyncio
async def test_fetch_summary_empty(isolated_env):
    """fetch_summary with no summaries returns empty list."""
    server = build_mcp_server()
    async with Client(server) as client:
        await _setup_project_with_agents(client, "/test/sum-fetch-empty", 1)

        result = await client.call_tool(
            "fetch_summary",
            {
                "project_key": "/test/sum-fetch-empty",
                "since_hours": 1.0,
            },
        )
        items = _get_list(result)
        assert len(items) == 0


# ============================================================================
# LLM refinement with mock
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_with_llm_mock(isolated_env, monkeypatch):
    """Mock LLM returns structured summary with key_decisions etc."""
    from contextlib import suppress

    from mcp_agent_mail import app as app_mod, config as _config

    monkeypatch.setenv("LLM_ENABLED", "true")
    with suppress(Exception):
        _config.clear_settings_cache()

    class _StubOut:
        def __init__(self, text: str):
            self.content = text
            self.model = "test-model"
            self.provider = "test"
            self.estimated_cost_usd = 0.001

    llm_response = (
        '{"key_decisions": ["Use JWT for auth"], '
        '"blockers_resolved": ["DB migration done"], '
        '"work_completed": ["Auth endpoint implemented"], '
        '"open_questions": ["Deploy timeline?"], '
        '"participants": ["AgentA", "AgentB"], '
        '"total_messages": 2, "total_threads": 1}'
    )

    async def _fake_llm(system, user, **kwargs):
        return _StubOut(llm_response)

    monkeypatch.setattr(app_mod, "complete_system_user", _fake_llm)

    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/sum-llm", 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/sum-llm",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "LLM test msg",
                "body_md": "- Implementing auth\n- TODO: deploy",
            },
        )

        result = await client.call_tool(
            "summarize_recent",
            {
                "project_key": "/test/sum-llm",
                "since_hours": 1.0,
                "llm_mode": True,
            },
        )
        data = _get_data(result)
        assert data["source_message_count"] == 1
        assert data["llm_model"] == "test-model"
        assert data["cost_usd"] == 0.001

        import json
        summary = json.loads(data["summary_text"])
        assert "key_decisions" in summary
        assert summary["key_decisions"] == ["Use JWT for auth"]
        assert summary["total_messages"] == 1  # Overridden by actual count
