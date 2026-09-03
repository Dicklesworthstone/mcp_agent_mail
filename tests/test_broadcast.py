"""Tests for broadcast topic threads (bd-26w).

Covers:
- Broadcast expansion to all agents (excluding sender)
- Broadcast with explicit recipients errors
- Broadcast respects contact_policy (block_all skipped)
- Topic field stored in messages
- Topic filtering in fetch_inbox
- fetch_topic tool
- Broadcast with topic combined
- Empty project broadcast (no recipients)
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

logger = logging.getLogger(__name__)


def _get_data(result):
    """Extract data dict from tool result (dict-returning tools like send_message)."""
    # FastMCP Client: structured_content["result"] gives plain Python objects
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
    """Extract list data from tool result (list-returning tools like fetch_inbox).

    Uses structured_content["result"] which returns plain dicts,
    unlike result.data which returns pydantic Root objects.
    """
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


# ============================================================================
# Helper: register N agents in a project
# ============================================================================


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
# Broadcast expansion
# ============================================================================


@pytest.mark.asyncio
async def test_broadcast_expands_to_all_agents(isolated_env):
    """broadcast=true with empty 'to' should deliver to all agents except sender."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast", 4)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcast",
                "sender_name": sender,
                "to": [],
                "subject": "Hello everyone",
                "body_md": "Broadcast test",
                "broadcast": True,
            },
        )
        data = _get_data(result)
        assert "deliveries" in data, f"Expected deliveries in result: {data}"
        payload = data["deliveries"][0]["payload"]
        # All 3 other agents should be recipients
        recipients = payload.get("to", [])
        assert len(recipients) == 3, f"Expected 3 recipients, got {len(recipients)}: {recipients}"
        assert sender not in recipients, "Sender should not be in broadcast recipients"
        for name in names[1:]:
            assert name in recipients, f"Expected {name} in recipients"


@pytest.mark.asyncio
async def test_broadcast_with_explicit_recipients_errors(isolated_env):
    """broadcast=true with non-empty 'to' should raise an error."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast-err", 2)

        with pytest.raises(Exception, match="mutually exclusive"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/bcast-err",
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Should fail",
                    "body_md": "Error test",
                    "broadcast": True,
                },
            )


@pytest.mark.asyncio
async def test_broadcast_excludes_sender(isolated_env):
    """Sender should never appear in their own broadcast recipient list."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast-self", 2)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcast-self",
                "sender_name": sender,
                "to": [],
                "subject": "Self-exclusion test",
                "body_md": "Should not include sender",
                "broadcast": True,
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        assert sender not in payload["to"]


@pytest.mark.asyncio
async def test_broadcast_respects_block_all_policy(isolated_env):
    """Agents with contact_policy=block_all should be excluded from broadcasts."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast-block", 3)
        sender = names[0]
        blocked_agent = names[1]

        # Set one agent to block_all
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": "/test/bcast-block",
                "agent_name": blocked_agent,
                "policy": "block_all",
            },
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcast-block",
                "sender_name": sender,
                "to": [],
                "subject": "Policy test",
                "body_md": "Should skip blocked agent",
                "broadcast": True,
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        recipients = payload["to"]
        assert blocked_agent not in recipients, "block_all agent should be excluded"
        assert len(recipients) == 1, f"Expected 1 recipient (excluding sender + blocked), got {len(recipients)}"


@pytest.mark.asyncio
async def test_broadcast_empty_project(isolated_env):
    """Broadcast with only sender registered should produce empty recipients."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast-empty", 1)
        sender = names[0]

        # broadcast with no other agents - should not error
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcast-empty",
                "sender_name": sender,
                "to": [],
                "subject": "Lonely broadcast",
                "body_md": "No one to receive this",
                "broadcast": True,
            },
        )
        # With no recipients, _deliver_message should not be called (no deliveries)
        data = _get_data(result)
        deliveries = data.get("deliveries", [])
        assert len(deliveries) == 0, f"Expected 0 deliveries for solo agent, got {len(deliveries)}"


# ============================================================================
# Topic storage and filtering
# ============================================================================


@pytest.mark.asyncio
async def test_topic_stored_in_message(isolated_env):
    """Message with topic param should have topic field in DB and payload."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-store", 2)

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-store",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Architecture discussion",
                "body_md": "Let's discuss",
                "topic": "architecture",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        assert payload.get("topic") == "architecture"


@pytest.mark.asyncio
async def test_topic_filtering_inbox(isolated_env):
    """fetch_inbox with topic filter returns only matching messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-filter", 2)
        sender, receiver = names[0], names[1]

        # Send messages with different topics
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-filter",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Topic A",
                "body_md": "Message about blockers",
                "topic": "blockers",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-filter",
                "sender_name": sender,
                "to": [receiver],
                "subject": "Topic B",
                "body_md": "Message about releases",
                "topic": "releases",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-filter",
                "sender_name": sender,
                "to": [receiver],
                "subject": "No topic",
                "body_md": "Regular message",
            },
        )

        # Filter by topic=blockers
        result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/topic-filter",
                "agent_name": receiver,
                "topic": "blockers",
                "include_bodies": True,
            },
        )
        items = _get_list(result)
        assert len(items) == 1
        assert items[0]["subject"] == "Topic A"
        assert items[0]["topic"] == "blockers"


@pytest.mark.asyncio
async def test_topic_filtering_empty(isolated_env):
    """fetch_inbox with non-existent topic returns empty list."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-empty", 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-empty",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "A message",
                "body_md": "body",
                "topic": "existing-topic",
            },
        )

        result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/topic-empty",
                "agent_name": names[1],
                "topic": "nonexistent",
            },
        )
        items = _get_list(result)
        assert len(items) == 0


@pytest.mark.asyncio
async def test_invalid_topic_rejected(isolated_env):
    """Topic with invalid characters should be rejected."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-invalid", 2)

        # FastMCP may raise or return error-flagged result depending on version
        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/topic-invalid",
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Bad topic",
                    "body_md": "body",
                    "topic": "spaces not allowed",
                },
            )
            # If no exception, check is_error flag or error in content
            assert getattr(result, "is_error", False) or "INVALID_TOPIC" in str(result), (
                f"Expected INVALID_TOPIC error, got: {result}"
            )
        except Exception as exc:
            assert "INVALID_TOPIC" in str(exc) or "topic" in str(exc).lower(), (
                f"Expected topic validation error, got: {exc}"
            )


@pytest.mark.asyncio
async def test_topic_allows_dotted_bead_ids(isolated_env):
    """Dotted beads_rust hierarchical IDs (e.g. ``br-abc.1``) are valid topics (issue #165)."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-dotted", 2)

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/topic-dotted",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Child bead coordination",
                "body_md": "Working on the child bead.",
                "topic": "br-abc.1",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        assert payload.get("topic") == "br-abc.1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_topic",
    [".", "..", "../foo", ".hidden", "a/b", "spaces not allowed", "-leading-hyphen", "_leading_underscore"],
)
async def test_topic_rejects_traversal_and_unsafe_shapes(isolated_env, bad_topic):
    """Path-traversal / dotfile / leading-non-alphanumeric topics are rejected (issue #165 guard)."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/topic-traversal", 2)

        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/topic-traversal",
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Bad topic",
                    "body_md": "body",
                    "topic": bad_topic,
                },
            )
            assert getattr(result, "is_error", False) or "INVALID_TOPIC" in str(result), (
                f"Expected INVALID_TOPIC error for {bad_topic!r}, got: {result}"
            )
        except Exception as exc:
            assert "INVALID_TOPIC" in str(exc) or "topic" in str(exc).lower(), (
                f"Expected topic validation error for {bad_topic!r}, got: {exc}"
            )


# ============================================================================
# fetch_topic tool
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_topic_tool(isolated_env):
    """fetch_topic returns all messages with matching topic across all senders."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/fetch-topic", 3)

        # Two different senders send messages with the same topic
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/fetch-topic",
                "sender_name": names[0],
                "to": [names[2]],
                "subject": "From agent 0",
                "body_md": "First message",
                "topic": "standup",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/fetch-topic",
                "sender_name": names[1],
                "to": [names[2]],
                "subject": "From agent 1",
                "body_md": "Second message",
                "topic": "standup",
            },
        )
        # Different topic (should not appear)
        await client.call_tool(
            "send_message",
            {
                "project_key": "/test/fetch-topic",
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Different topic",
                "body_md": "Not standup",
                "topic": "other",
            },
        )

        result = await client.call_tool(
            "fetch_topic",
            {
                "project_key": "/test/fetch-topic",
                "topic_name": "standup",
            },
        )
        items = _get_list(result)
        assert len(items) == 2
        subjects = {m["subject"] for m in items}
        assert "From agent 0" in subjects
        assert "From agent 1" in subjects


@pytest.mark.asyncio
async def test_broadcast_with_topic(isolated_env):
    """Broadcast + topic: all agents receive topic-tagged message."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, "/test/bcast-topic", 3)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/bcast-topic",
                "sender_name": sender,
                "to": [],
                "subject": "Daily standup",
                "body_md": "What are you working on?",
                "broadcast": True,
                "topic": "standup",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        assert payload.get("topic") == "standup"
        assert len(payload["to"]) == 2  # 3 agents minus sender

        # Verify message visible via fetch_topic
        topic_result = await client.call_tool(
            "fetch_topic",
            {
                "project_key": "/test/bcast-topic",
                "topic_name": "standup",
            },
        )
        items = _get_list(topic_result)
        assert len(items) == 1
        assert items[0]["subject"] == "Daily standup"


@pytest.mark.asyncio
async def test_broadcast_skips_contact_gated_recipients_without_creating_requests(isolated_env):
    """A broadcast must not fire contact requests at every unlinked agent.

    PR #271: broadcast recipients are machine-expanded, not chosen by the
    sender. Before this fix, an expanded recipient whose contact policy needed
    approval was pushed through the auto-handshake path — creating a pending
    contact request and an ack_required intro message for it — and the whole
    send then failed with CONTACT_REQUIRED, so the eligible recipients got
    nothing. The broadcast is now best-effort: eligible recipients receive the
    message and the refusals come back in ``broadcast_skipped``.
    """
    server = build_mcp_server()
    project_key = "/test/bcast-contact-gate"

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        registered: list[tuple[str, str]] = []
        for i in range(3):
            data = _get_data(
                await client.call_tool(
                    "register_agent",
                    {
                        "project_key": project_key,
                        "program": "test-prog",
                        "model": "test-model",
                        "task_description": f"agent-{i}",
                    },
                )
            )
            registered.append((data["name"], data["registration_token"]))
        (
            (sender, sender_token),
            (open_recipient, open_token),
            (gated_recipient, gated_token),
        ) = registered
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": open_recipient,
                "policy": "open",
            },
        )
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": gated_recipient,
                "policy": "contacts_only",
            },
        )

    # A fresh session: no recipient is bound to it, so the in-session
    # auto-approval shortcut cannot apply and the contact gate is real.
    async with Client(server) as client:
        result = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender,
                "to": [],
                "subject": "Broadcast across a contact boundary",
                "body_md": "Only the open recipient should get this.",
                "broadcast": True,
                "sender_token": sender_token,
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["payload"]
        assert payload["to"] == [open_recipient], f"unexpected recipients: {payload['to']}"
        assert data.get("broadcast_skipped") == [
            {"agent": gated_recipient, "reason": "contact_required"}
        ], f"unexpected broadcast_skipped: {data.get('broadcast_skipped')}"

        # The gated recipient was left entirely alone: no broadcast message and
        # no auto-created contact-request intro in its inbox.
        gated_inbox = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": project_key,
                    "agent_name": gated_recipient,
                    "registration_token": gated_token,
                },
            )
        )
        assert gated_inbox == [], f"gated recipient should have an empty inbox: {gated_inbox}"

        # The eligible recipient really received it.
        open_inbox = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": project_key,
                    "agent_name": open_recipient,
                    "registration_token": open_token,
                },
            )
        )
        assert [m["subject"] for m in open_inbox] == ["Broadcast across a contact boundary"]
