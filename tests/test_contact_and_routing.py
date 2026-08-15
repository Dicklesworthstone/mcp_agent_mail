from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_db_health_status, get_session
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
        assert get_db_health_status()["pool"]["checked_out"] == 0


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
        assert p1.id is not None
        assert p2.id is not None
        a_sender = Agent(
            project_id=p1.id,
            name="Alpha",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="alpha-token",
        )
        b_recv = Agent(
            project_id=p2.id,
            name="Receiver",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="receiver-token",
        )
        s.add(a_sender)
        s.add(b_recv)
        await s.commit()
        await s.refresh(a_sender)
        await s.refresh(b_recv)
        assert a_sender.id is not None
        assert b_recv.id is not None
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
                "sender_token": "alpha-token",
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


@pytest.mark.asyncio
async def test_cross_project_routing_without_approved_link_when_disabled(isolated_env, monkeypatch):
    monkeypatch.setenv("CROSS_PROJECT_CONTACT_ENFORCEMENT_ENABLED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    await ensure_schema()
    async with get_session() as s:
        backend = Project(slug="backend", human_key="Backend")
        frontend = Project(slug="frontend", human_key="Frontend")
        s.add_all([backend, frontend])
        await s.commit()
        await s.refresh(backend)
        await s.refresh(frontend)
        assert backend.id is not None and frontend.id is not None
        sender = Agent(
            project_id=backend.id,
            name="cross-trust-sender",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="sender-token",
        )
        recipient = Agent(
            project_id=frontend.id,
            name="cross-trust-receiver",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="recipient-token",
        )
        s.add_all([sender, recipient])
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "cross-trust-sender",
                "sender_token": "sender-token",
                "to": ["project:Frontend#cross-trust-receiver"],
                "subject": "Cross",
                "body_md": "hello",
            },
        )
        deliveries = sent.data.get("deliveries") or []
        assert any(delivery.get("project") == "Frontend" for delivery in deliveries)
        message_id = deliveries[0]["payload"]["id"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": "Frontend",
                "message_id": message_id,
                "sender_name": "cross-trust-receiver",
                "sender_token": "recipient-token",
                "body_md": "ack",
            },
        )
        assert any(delivery.get("project") == "Backend" for delivery in reply.data.get("deliveries") or [])


@pytest.mark.asyncio
async def test_bare_name_prefers_cross_project_over_local_shadow(isolated_env):
    """Regression for PR #138 Bug 1 (send_message shadow-routing).

    When a bare recipient name has BOTH a local agent (e.g. a stale shadow
    auto-registered by a prior auto_contact_if_blocked cycle) AND an approved
    cross-project AgentLink, send_message must route to the cross-project
    recipient — silently delivering to the local shadow has caused production
    message loss.
    """
    await ensure_schema()
    async with get_session() as s:
        p_local = Project(slug="geordi", human_key="Geordi")
        p_remote = Project(slug="servitor", human_key="Servitor")
        s.add_all([p_local, p_remote])
        await s.commit()
        await s.refresh(p_local)
        await s.refresh(p_remote)
        assert p_local.id is not None and p_remote.id is not None

        sender = Agent(
            project_id=p_local.id,
            name="Geordi",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="geordi-token",
        )
        # Local "shadow" Adama — same name as the real cross-project recipient.
        # Simulates the residue of a prior auto_contact_if_blocked + auto-register
        # cycle that left a placeholder local agent in this project.
        local_shadow = Agent(
            project_id=p_local.id,
            name="Adama",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="local-shadow-token",
        )
        remote_real = Agent(
            project_id=p_remote.id,
            name="Adama",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="remote-adama-token",
        )
        s.add_all([sender, local_shadow, remote_real])
        await s.commit()
        await s.refresh(sender)
        await s.refresh(local_shadow)
        await s.refresh(remote_real)
        assert sender.id is not None
        assert local_shadow.id is not None
        assert remote_real.id is not None

        link = AgentLink(
            a_project_id=p_local.id,
            a_agent_id=sender.id,
            b_project_id=p_remote.id,
            b_agent_id=remote_real.id,
            status="approved",
        )
        s.add(link)
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Geordi",
                "sender_name": "Geordi",
                "sender_token": "geordi-token",
                "to": ["Adama"],  # bare name — used to silently hit local shadow
                "subject": "BareName",
                "body_md": "should reach Servitor, not the local shadow",
            },
        )
        deliveries = res.data.get("deliveries") or []
        delivered_projects = [d.get("project") for d in deliveries]
        assert "Servitor" in delivered_projects, (
            f"bare-name send must reach the cross-project recipient via "
            f"approved AgentLink; got deliveries={delivered_projects!r}"
        )
        assert "Geordi" not in delivered_projects, (
            f"bare-name send must NOT silently land in the local shadow "
            f"mailbox when an approved cross-project link exists; got "
            f"deliveries={delivered_projects!r}"
        )

        storage_root = Path(get_settings().storage.root).expanduser().resolve()
        servitor_dir = storage_root / "projects" / "servitor" / "messages"
        assert any(servitor_dir.rglob("*.md")), "no message archived in Servitor"


@pytest.mark.asyncio
async def test_bare_name_prefers_local_when_no_cross_project_link(isolated_env):
    """Tenant-isolation guard: when there is no approved cross-project link
    for a bare name, the existing local-resolution path must keep working."""
    await ensure_schema()
    async with get_session() as s:
        p = Project(slug="solo", human_key="Solo")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        assert p.id is not None
        sender = Agent(
            project_id=p.id,
            name="Sender",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="sender-token",
        )
        recipient = Agent(
            project_id=p.id,
            name="Recipient",
            program="codex",
            model="gpt-5",
            task_description="",
            registration_token="recipient-token",
            contact_policy="open",  # skip contact-policy block for the test
        )
        s.add_all([sender, recipient])
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Solo",
                "sender_name": "Sender",
                "sender_token": "sender-token",
                "to": ["Recipient"],
                "subject": "Local",
                "body_md": "stays local",
            },
        )
        deliveries = res.data.get("deliveries") or []
        delivered_projects = [d.get("project") for d in deliveries]
        assert delivered_projects == ["Solo"], (
            f"local-only routing regressed; deliveries={delivered_projects!r}"
        )
