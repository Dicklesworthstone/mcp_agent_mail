from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, AgentLink, Project


@pytest.mark.asyncio
async def test_contact_auto_allow_same_thread(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Tighten policy to require contact; enforcement enabled by default
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "contacts_only"},
        )

        # Seed thread with ack-required message (bypasses enforcement)
        first = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "ThreadSeed",
                "body_md": "seed",
                "ack_required": True,
            },
        )
        deliveries = first.data.get("deliveries") or []
        thread_id = deliveries[0]["payload"].get("thread_id") or deliveries[0]["payload"].get("id")
        assert thread_id

        # Beta replies (becomes a sender on the same thread)
        # Use reply_message which preserves thread id
        # Find the seed message id from storage by reading the response payload id
        seed_id = deliveries[0]["payload"]["id"]
        rep = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": seed_id,
                "sender_name": "BlueLake",
                "body_md": "ack",
            },
        )
        assert rep.data["deliveries"]

        # Alpha can now send non-ack message in the same thread to Beta due to auto-allow
        third = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Followup",
                "body_md": "details",
                "thread_id": str(thread_id),
                "ack_required": False,
            },
        )
        assert (third.data.get("deliveries") or [{}])[0].get("payload", {}).get("subject") == "Followup"


@pytest.mark.asyncio
async def test_external_cross_project_routing(isolated_env):
    # Prepare DB state directly for an approved cross-project link
    await ensure_schema()
    async with get_session() as s:
        p1 = Project(slug="backend", human_key="Backend")
        p2 = Project(slug="ops", human_key="Ops")
        s.add(p1)
        s.add(p2)
        await s.commit()
        await s.refresh(p1)
        await s.refresh(p2)
        a_sender = Agent(project_id=p1.id, name="Alpha", program="codex", model="gpt-5", task_description="")
        b_recv = Agent(project_id=p2.id, name="Receiver", program="codex", model="gpt-5", task_description="")
        s.add(a_sender)
        s.add(b_recv)
        await s.commit()
        await s.refresh(a_sender)
        await s.refresh(b_recv)
        link = AgentLink(
            a_project_id=p1.id,
            a_agent_id=a_sender.id,
            b_project_id=p2.id,
            b_agent_id=b_recv.id,
            status="approved",
        )
        s.add(link)
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        # Route explicitly to Ops#Receiver
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "Alpha",
                "to": ["project:ops#Receiver"],
                "subject": "Cross",
                "body_md": "hello",
            },
        )
        deliveries = res.data.get("deliveries") or []
        # Should deliver to Ops project via external routing bucket
        assert any(d.get("project") == "Ops" for d in deliveries)

        # Verify archive in Ops contains message file
        storage_root = Path(get_settings().storage.root).expanduser().resolve()
        ops_dir = storage_root / "projects" / "ops" / "messages"
        assert any(ops_dir.rglob("*.md"))

