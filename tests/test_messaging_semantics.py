from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_db_health_status


@pytest.mark.asyncio
async def test_reply_message_inherits_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        orig_id = int(msg.get("id"))
        # Reply
        r = await client.call_tool(
            "reply_message",
            {"project_key": "Backend", "message_id": orig_id, "sender_name": "BlueLake", "body_md": "ack"},
        )
        rdata = r.data
        expected_thread = msg.get("thread_id") or str(orig_id)
        assert rdata.get("thread_id") == expected_thread
        assert str(rdata.get("reply_to")) == str(orig_id)
        # Subject on delivery payload should be prefixed
        deliveries = rdata.get("deliveries") or []
        assert deliveries
        subj = deliveries[0].get("payload", {}).get("subject", "")
        assert subj.lower().startswith("re:")


@pytest.mark.asyncio
async def test_reply_message_explicit_empty_to_does_not_restore_default_recipient(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/reply-empty-to"})
        await client.call_tool(
            "register_agent",
            {"project_key": "ReplyEmptyTo", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        original = await client.call_tool(
            "send_message",
            {
                "project_key": "ReplyEmptyTo",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
            },
        )
        original_payload = (original.data.get("deliveries") or [{}])[0].get("payload", {})
        original_id = int(original_payload["id"])

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": "ReplyEmptyTo",
                "message_id": original_id,
                "sender_name": "BlueLake",
                "to": [],
                "body_md": "no recipients on purpose",
            },
        )
        assert reply.data["count"] == 0
        assert reply.data["deliveries"] == []
        assert reply.data["thread_id"] == (original_payload.get("thread_id") or str(original_id))
        assert reply.data["reply_to"] == original_id


@pytest.mark.asyncio
async def test_mark_read_then_ack_updates_state(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "RedStone"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["RedStone"],
                "subject": "AckPlease",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        mr = await client.call_tool(
            "mark_message_read",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        assert mr.data.get("read") is True and isinstance(mr.data.get("read_at"), str)

        ack = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        assert ack.data.get("acknowledged") is True
        assert isinstance(ack.data.get("acknowledged_at"), str)
        assert isinstance(ack.data.get("read_at"), str)


@pytest.mark.asyncio
async def test_acknowledge_idempotent_multiple_calls(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "RedStone"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["RedStone"],
                "subject": "AckTwice",
                "body_md": "hello",
                "ack_required": True,
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("payload", {})
        mid = int(msg.get("id"))

        first = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        first_ack_at = first.data.get("acknowledged_at")
        assert first.data.get("acknowledged") is True and isinstance(first_ack_at, str)

        second = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "RedStone", "message_id": mid},
        )
        # Timestamps should remain the same (idempotent)
        assert second.data.get("acknowledged_at") == first_ack_at


@pytest.mark.asyncio
async def test_send_message_requires_sender_token_across_sessions(isolated_env):
    """A fresh session cannot impersonate an existing sender without sender_token."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/spoof-send"})
        sender = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/spoof-send", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        sender_token = sender.data["registration_token"]

    async with Client(server) as attacker_client:
        with pytest.raises(ToolError) as exc_info:
            await attacker_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/spoof-send",
                    "sender_name": "GreenCastle",
                    "to": ["GreenCastle"],
                    "subject": "Forged",
                    "body_md": "This should fail",
                },
            )
        assert "sender_token" in str(exc_info.value)

    async with Client(server) as sender_client:
        result = await sender_client.call_tool(
            "send_message",
                {
                    "project_key": "/security/spoof-send",
                    "sender_name": "GreenCastle",
                    "sender_token": sender_token,
                    "to": ["GreenCastle"],
                    "subject": "Legit",
                    "body_md": "This should succeed",
                },
        )
    assert result.data["verified_sender"] is True
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_send_message_auto_contact_requests_pending_approval_without_target_auth(isolated_env):
    """auto_contact_if_blocked should create a pending request, not pretend to auto-approve."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-pending"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-pending", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-pending", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError) as exc_info:
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/auto-contact-pending",
                    "sender_name": "GreenCastle",
                    "sender_token": green_token,
                    "to": ["BlueLake"],
                    "subject": "Need approval",
                    "body_md": "please let me in",
                    "auto_contact_if_blocked": True,
                },
            )
        assert "Pending contact requests were created for: BlueLake" in str(exc_info.value)

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": "/security/auto-contact-pending",
                "agent_name": "GreenCastle",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "BlueLake" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/security/auto-contact-pending",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert any(item["subject"] == "Contact request from GreenCastle" for item in messages)


@pytest.mark.asyncio
async def test_send_message_explicit_false_disables_local_auto_contact(isolated_env):
    """Explicit false should override the server default and avoid creating contact requests."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-disabled"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-disabled", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-disabled", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError):
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/auto-contact-disabled",
                    "sender_name": "GreenCastle",
                    "sender_token": green_token,
                    "to": ["BlueLake"],
                    "subject": "No auto contact",
                    "body_md": "stay blocked",
                    "auto_contact_if_blocked": False,
                },
            )

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": "/security/auto-contact-disabled",
                "agent_name": "GreenCastle",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert not any(item["to"] == "BlueLake" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/security/auto-contact-disabled",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert not any(item["subject"] == "Contact request from GreenCastle" for item in messages)


@pytest.mark.asyncio
async def test_send_message_auto_contact_auto_approves_when_target_is_authenticated_in_session(isolated_env):
    """Same-session authenticated agents can still use the one-step auto-approval path."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/security/auto-contact-approved"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-approved", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-approved", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/security/auto-contact-approved",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Auto approved",
                "body_md": "same session works",
                "auto_contact_if_blocked": True,
            },
        )
        assert result.data["count"] == 1

        contacts = await client.call_tool(
            "list_contacts",
            {
                "project_key": "/security/auto-contact-approved",
                "agent_name": "GreenCastle",
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "BlueLake" and item["status"] == "approved" for item in contact_items)


@pytest.mark.asyncio
async def test_send_message_auto_contact_requests_cross_project_approval_without_target_auth(isolated_env):
    """Cross-project auto-contact should create a pending request when only the sender is authenticated."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-xproj-backend"})
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-xproj-frontend"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-xproj-backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-xproj-frontend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError) as exc_info:
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/auto-contact-xproj-backend",
                    "sender_name": "GreenCastle",
                    "sender_token": green_token,
                    "to": ["BlueLake@/security/auto-contact-xproj-frontend"],
                    "subject": "Need cross-project approval",
                    "body_md": "please link us",
                    "auto_contact_if_blocked": True,
                },
            )
        assert "pending external contact requests were created for BlueLake@/security/auto-contact-xproj-frontend" in str(exc_info.value)

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": "/security/auto-contact-xproj-backend",
                "agent_name": "GreenCastle",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "BlueLake" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/security/auto-contact-xproj-frontend",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert any(item["subject"] == "Contact request from GreenCastle" for item in messages)


@pytest.mark.asyncio
async def test_send_message_explicit_false_disables_cross_project_auto_contact(isolated_env):
    """Explicit false should prevent the external auto-handshake path from creating requests."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-false-backend"})
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/auto-contact-false-frontend"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-false-backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-false-frontend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError):
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": "/security/auto-contact-false-backend",
                    "sender_name": "GreenCastle",
                    "sender_token": green_token,
                    "to": ["BlueLake@/security/auto-contact-false-frontend"],
                    "subject": "No external auto contact",
                    "body_md": "stay blocked",
                    "auto_contact_if_blocked": False,
                },
            )

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": "/security/auto-contact-false-backend",
                "agent_name": "GreenCastle",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert not any(item["to"] == "BlueLake" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/security/auto-contact-false-frontend",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert not any(item["subject"] == "Contact request from GreenCastle" for item in messages)


@pytest.mark.asyncio
async def test_send_message_cross_project_auto_contact_preserves_recipient_kind(isolated_env):
    """In-session cross-project auto-approval must preserve whether the target was TO/CC/BCC."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/security/auto-contact-kind-backend"})
        await client.call_tool("ensure_project", {"human_key": "/security/auto-contact-kind-frontend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-kind-backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/auto-contact-kind-frontend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/security/auto-contact-kind-backend",
                "sender_name": "GreenCastle",
                "to": ["GreenCastle"],
                "bcc": ["BlueLake@/security/auto-contact-kind-frontend"],
                "subject": "Cross-project BCC",
                "body_md": "recipient kind must survive auto-approval",
                "auto_contact_if_blocked": True,
            },
        )
        assert result.data["count"] == 2

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/security/auto-contact-kind-frontend",
                "agent_name": "BlueLake",
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        delivered = next(item for item in messages if item["subject"] == "Cross-project BCC")
        assert delivered["kind"] == "bcc"
        assert delivered["body_md"] == "recipient kind must survive auto-approval"


@pytest.mark.asyncio
async def test_reply_message_enforces_local_contact_policy_for_new_recipient(isolated_env):
    """reply_message should not bypass local contacts_only policy for a newly added recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/security/reply-contact-policy"})
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-contact-policy", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-contact-policy", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-contact-policy", "program": "codex", "model": "gpt-5", "name": "PurpleBear"},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/security/reply-contact-policy", "agent_name": "PurpleBear", "policy": "contacts_only"},
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": "/security/reply-contact-policy",
                "sender_name": "BlueLake",
                "to": ["GreenCastle"],
                "subject": "Seed",
                "body_md": "start thread",
            },
        )
        seed_id = (seed.data.get("deliveries") or [])[0]["payload"]["id"]

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": "/security/reply-contact-policy",
                    "message_id": seed_id,
                    "sender_name": "GreenCastle",
                    "to": ["PurpleBear"],
                    "body_md": "looping in a new recipient",
                },
            )
        assert "Contact approval required for recipients: PurpleBear" in str(exc_info.value)


@pytest.mark.asyncio
async def test_reply_message_supports_agent_at_project_external_address(isolated_env):
    """reply_message should route approved cross-project recipients addressed as Agent@project."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/security/reply-xproj-backend"})
        await client.call_tool("ensure_project", {"human_key": "/security/reply-xproj-ops"})
        green = await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-xproj-backend", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-xproj-backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        ops = await client.call_tool(
            "register_agent",
            {"project_key": "/security/reply-xproj-ops", "program": "codex", "model": "gpt-5", "name": "OpsBot"},
        )
        green_token = green.data["registration_token"]
        ops_token = ops.data["registration_token"]
        ops_name = ops.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "/security/reply-xproj-backend",
                "requester": "GreenCastle",
                "target": ops_name,
                "to_project": "/security/reply-xproj-ops",
                "auto_accept": True,
                "requester_registration_token": green_token,
                "target_registration_token": ops_token,
            },
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": "/security/reply-xproj-backend",
                "sender_name": "BlueLake",
                "to": ["GreenCastle"],
                "subject": "Seed",
                "body_md": "start thread",
            },
        )
        seed_id = (seed.data.get("deliveries") or [])[0]["payload"]["id"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": "/security/reply-xproj-backend",
                "message_id": seed_id,
                "sender_name": "GreenCastle",
                "to": [f"{ops_name}@/security/reply-xproj-ops"],
                "body_md": "routing externally from a reply",
            },
        )
        assert any(delivery["project"] == "/security/reply-xproj-ops" for delivery in reply.data["deliveries"])
        assert get_db_health_status()["pool"]["checked_out"] == 0


@pytest.mark.asyncio
async def test_cross_project_sender_identity_does_not_collide_with_same_name_local_agent(isolated_env):
    """Cross-project mail must not leak into a same-name local agent's outbox or contact auto-allow heuristics."""
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/xproj-origin-backend"
        ops_key = "/security/xproj-origin-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        source = await client.call_tool(
            "register_agent",
            {"project_key": backend_key, "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        receiver = await client.call_tool(
            "register_agent",
            {"project_key": ops_key, "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        lookalike = await client.call_tool(
            "register_agent",
            {"project_key": ops_key, "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        source_token = source.data["registration_token"]
        receiver_token = receiver.data["registration_token"]
        lookalike_token = lookalike.data["registration_token"]

        await client.call_tool(
            "set_contact_policy",
            {"project_key": ops_key, "agent_name": "GreenCastle", "policy": "contacts_only"},
        )
        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "GreenCastle",
                "target": "BlueLake",
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": source_token,
                "target_registration_token": receiver_token,
            },
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "GreenCastle",
                "sender_token": source_token,
                "to": [f"BlueLake@{ops_key}"],
                "subject": "Cross-project origin",
                "body_md": "hello from the real backend sender",
                "thread_id": "XPROJ-IDENTITY-1",
            },
        )
        ext_delivery = next(delivery for delivery in sent.data["deliveries"] if delivery["project"] == ops_key)
        ext_payload = ext_delivery["payload"]
        ext_message_id = ext_payload["id"]

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": ops_key,
                "agent_name": "BlueLake",
                "registration_token": receiver_token,
                "include_bodies": True,
            },
        )
        delivered = next(item for item in inbox.structured_content["result"] if item["id"] == ext_message_id)
        assert delivered["from"] == "GreenCastle"
        assert delivered["from_project"] == backend_key
        assert delivered["from_address"].endswith("#GreenCastle")

        outbox_blocks = await client.read_resource(
            f"resource://outbox/GreenCastle?project={ops_key}&agent_token={lookalike_token}"
        )
        outbox_payload = json.loads(outbox_blocks[0].text or "{}")
        assert outbox_payload["count"] == 0

        msg_blocks = await client.read_resource(
            f"resource://message/{ext_message_id}?project={ops_key}&agent=BlueLake&agent_token={receiver_token}"
        )
        msg_payload = json.loads(msg_blocks[0].text or "{}")
        assert msg_payload["from"] == "GreenCastle"
        assert msg_payload["from_project"] == backend_key

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": ops_key,
                    "message_id": ext_message_id,
                    "sender_name": "BlueLake",
                    "sender_token": receiver_token,
                    "to": ["GreenCastle"],
                    "body_md": "trying to loop in the local lookalike",
                },
            )
        assert "Contact approval required for recipients: GreenCastle" in str(exc_info.value)

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": ops_key,
                "message_id": ext_message_id,
                "sender_name": "BlueLake",
                "sender_token": receiver_token,
                "body_md": "replying to the actual external sender",
            },
        )
        backend_delivery = next(delivery for delivery in reply.data["deliveries"] if delivery["project"] == backend_key)
        backend_payload = backend_delivery["payload"]
        assert backend_payload["from"] == "BlueLake"
        assert backend_payload["from_project"] == ops_key
        assert get_db_health_status()["pool"]["checked_out"] == 0


@pytest.mark.asyncio
async def test_send_message_does_not_count_external_delivery_errors_as_success(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/send-partial-backend"
        ops_key = "/security/send-partial-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        sender = await client.call_tool(
            "register_agent",
            {"project_key": backend_key, "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        receiver = await client.call_tool(
            "register_agent",
            {"project_key": ops_key, "program": "codex", "model": "gpt-5"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": ops_key, "program": "codex", "model": "gpt-5", "name": "RedStone"},
        )
        receiver_name = receiver.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "GreenCastle",
                "target": receiver_name,
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": sender.data["registration_token"],
                "target_registration_token": receiver.data["registration_token"],
            },
        )

        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": ops_key,
                "agent_name": "RedStone",
                "paths": [f"agents/{receiver_name}/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "GreenCastle",
                "sender_token": sender.data["registration_token"],
                "to": ["GreenCastle", f"{receiver_name}@{ops_key}"],
                "subject": "Partial delivery",
                "body_md": "one local write, one blocked external write",
            },
        )

        assert sent.data["count"] == 1
        assert [delivery["project"] for delivery in sent.data["deliveries"]] == [backend_key]
        delivery_errors = sent.data.get("delivery_errors") or []
        assert len(delivery_errors) == 1
        assert delivery_errors[0]["type"] == "FILE_RESERVATION_CONFLICT"
        assert delivery_errors[0]["project"] == ops_key


@pytest.mark.asyncio
async def test_reply_message_surfaces_external_delivery_failures(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/reply-failure-backend"
        ops_key = "/security/reply-failure-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        sender = await client.call_tool(
            "register_agent",
            {"project_key": backend_key, "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        receiver = await client.call_tool(
            "register_agent",
            {"project_key": ops_key, "program": "codex", "model": "gpt-5"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": backend_key, "program": "codex", "model": "gpt-5", "name": "RedStone"},
        )
        receiver_name = receiver.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "GreenCastle",
                "target": receiver_name,
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": sender.data["registration_token"],
                "target_registration_token": receiver.data["registration_token"],
            },
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "GreenCastle",
                "sender_token": sender.data["registration_token"],
                "to": [f"{receiver_name}@{ops_key}"],
                "subject": "Seed external thread",
                "body_md": "start the thread externally",
            },
        )
        ops_delivery = next(
            delivery for delivery in (seed.data.get("deliveries") or []) if delivery["project"] == ops_key
        )
        ops_message_id = ops_delivery["payload"]["id"]

        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": backend_key,
                "agent_name": "RedStone",
                "paths": ["agents/GreenCastle/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": ops_key,
                "message_id": ops_message_id,
                "sender_name": receiver_name,
                "sender_token": receiver.data["registration_token"],
                "body_md": "this cross-project reply should be blocked by the reservation",
            },
        )

        assert reply.data["count"] == 0
        error = reply.data.get("error") or {}
        assert error["type"] == "FILE_RESERVATION_CONFLICT"
        assert error["project"] == backend_key


@pytest.mark.asyncio
async def test_search_and_summarize_thread_respect_recipient_visibility(isolated_env):
    """Only senders/recipients, including BCC, can discover a private thread."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": "/security/private-thread"})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "GreenCastle"},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        purple = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "/security/private-thread", "program": "codex", "model": "gpt-5", "name": "PurpleBear"},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]
        purple_token = purple.data["registration_token"]

    async with Client(server) as sender_client:
        await sender_client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "/security/private-thread",
                "requester": "GreenCastle",
                "target": "BlueLake",
                "auto_accept": True,
                "requester_registration_token": green_token,
                "target_registration_token": blue_token,
            },
        )
        await sender_client.call_tool(
            "send_message",
            {
                "project_key": "/security/private-thread",
                "sender_name": "GreenCastle",
                "sender_token": green_token,
                "to": ["GreenCastle"],
                "bcc": ["BlueLake"],
                "subject": "Private plan",
                "body_md": "ultra-secret launch sequence",
                "thread_id": "SEC-THREAD-1",
            },
        )

    async with Client(server) as bcc_client:
        search_result = await bcc_client.call_tool(
            "search_messages",
            {
                "project_key": "/security/private-thread",
                "query": "ultra-secret",
                "agent_name": "BlueLake",
                "registration_token": blue_token,
            },
        )
        assert len(search_result.structured_content["result"]) == 1

        summary_result = await bcc_client.call_tool(
            "summarize_thread",
            {
                "project_key": "/security/private-thread",
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "BlueLake",
                "registration_token": blue_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 1
        assert len(summary_result.data["examples"]) == 1

    async with Client(server) as outsider_client:
        search_result = await outsider_client.call_tool(
            "search_messages",
            {
                "project_key": "/security/private-thread",
                "query": "ultra-secret",
                "agent_name": "PurpleBear",
                "registration_token": purple_token,
            },
        )
        assert search_result.structured_content["result"] == []

        summary_result = await outsider_client.call_tool(
            "summarize_thread",
            {
                "project_key": "/security/private-thread",
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "PurpleBear",
                "registration_token": purple_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 0
        assert summary_result.data["examples"] == []
