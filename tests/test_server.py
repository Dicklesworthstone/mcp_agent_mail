import pytest
from pathlib import Path

from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings


@pytest.mark.asyncio
async def test_messaging_flow(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        health = await client.call_tool("health_check", {})
        assert health.data["status"] == "ok"
        assert health.data["environment"] == "test"

        project = await client.call_tool("ensure_project", {"human_key": "Backend"})
        assert project.data["slug"] == "backend"

        agent = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
                "task_description": "testing",
            },
        )
        assert agent.data["name"] == "BlueLake"

        message = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Test",
                "body_md": "hello",
            },
        )
        assert message.data["subject"] == "Test"

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
            },
        )
        inbox_items = inbox.structured_content.get("result")
        assert isinstance(inbox_items, list)
        assert len(inbox_items) == 1
        assert inbox_items[0]["subject"] == "Test"

        resource_blocks = await client.read_resource("resource://project/backend")
        assert resource_blocks
        text_payload = resource_blocks[0].text
        assert "BlueLake" in text_payload

        storage_root = Path(get_settings().storage.root).resolve()
        profile = storage_root / "backend" / "agents" / "BlueLake" / "profile.json"
        assert profile.exists()
        message_file = list((storage_root / "backend" / "messages").rglob("*.md"))[0]
        assert "Test" in message_file.read_text()


@pytest.mark.asyncio
async def test_claim_conflicts_and_release(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "Alpha",
            },
        )
        await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "Beta",
            },
        )

        result = await client.call_tool(
            "claim_paths",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert result.data["granted"][0]["path_pattern"] == "src/app.py"

        conflict = await client.call_tool(
            "claim_paths",
            {
                "project_key": "Backend",
                "agent_name": "Beta",
                "paths": ["src/app.py"],
            },
        )
        assert conflict.data["conflicts"]

        release = await client.call_tool(
            "release_claims",
            {
                "project_key": "Backend",
                "agent_name": "Alpha",
                "paths": ["src/app.py"],
            },
        )
        assert release.data["released"] == 1


@pytest.mark.asyncio
async def test_search_and_summarize(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "Backend"})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "- TODO: implement FTS\n- ACTION: review claims",
            },
        )
        search = await client.call_tool(
            "search_messages",
            {"project_key": "Backend", "query": "FTS", "limit": 5},
        )
        assert any(item["subject"] == "Plan" for item in search.data)

        summary = await client.call_tool(
            "summarize_thread",
            {"project_key": "Backend", "thread_id": "1", "include_examples": True},
        )
        summary_data = summary.data["summary"]
        assert "TODO" in " ".join(summary_data["key_points"])
