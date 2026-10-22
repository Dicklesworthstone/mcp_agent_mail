from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from sqlalchemy import select, text
from sqlmodel import col

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_db_health_status, get_session
from mcp_agent_mail.models import Agent, AgentLink, Project


@pytest.fixture
def routing_env(isolated_env, monkeypatch):
    """Exercise registered punctuation identities with explicit contact decisions."""
    monkeypatch.setenv("AGENT_NAME_ENFORCEMENT_MODE", "strict")
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("MESSAGING_AUTO_REGISTER_RECIPIENTS", "false")
    monkeypatch.setenv("MESSAGING_AUTO_HANDSHAKE_ON_BLOCK", "false")
    clear_settings_cache()


async def _register_routing_agent(client: Client, project: str, name: str) -> dict[str, Any]:
    await client.call_tool("ensure_project", {"human_key": project})
    result = await client.call_tool(
        "register_agent",
        {"project_key": project, "program": "codex", "model": "gpt-5", "name": name},
    )
    assert result.data["name"] == name
    return result.data


async def _approve_routing_contact(
    client: Client, source_project: str, sender: str, target_project: str, recipient: str
) -> None:
    await client.call_tool(
        "request_contact",
        {
            "project_key": source_project,
            "from_agent": sender,
            "to_project": target_project,
            "to_agent": recipient,
        },
    )
    result = await client.call_tool(
        "respond_contact",
        {
            "project_key": target_project,
            "to_agent": recipient,
            "from_project": source_project,
            "from_agent": sender,
            "accept": True,
        },
    )
    assert result.data["approved"] is True


def _routing_address(style: str, project: str, name: str) -> str:
    if style == "project":
        return f"project:{project.lstrip('/')}#{name}"
    if style == "at":
        return f"{name}@{project}"
    return name


async def _assert_message_identity(body: str, sender_id: int, recipient_id: int) -> None:
    async with get_session() as session:
        rows = await session.execute(
            text(
                "SELECT m.sender_id, r.agent_id, m.project_id = a.project_id "
                "FROM messages m JOIN message_recipients r ON r.message_id = m.id "
                "JOIN agents a ON a.id = r.agent_id WHERE m.body_md = :body"
            ),
            {"body": body},
        )
        assert [tuple(row) for row in rows] == [(sender_id, recipient_id, 1)]


async def _routing_attempt(
    client: Client, tool: str, address: str, body: str, *, sender_name: str = "GreenCastle"
):
    arguments: dict[str, Any] = {
        "project_key": "/routing-a",
        "sender_name": sender_name,
        "to": [address],
        "body_md": body,
    }
    if tool == "send_message":
        arguments["subject"] = "Routing authorization"
    else:
        # Use a visible local message to exercise explicit reply recipients
        # without granting an unrelated external recipient contact approval.
        seed = await client.call_tool(
            "send_message",
            {
                "project_key": "/routing-a",
                "sender_name": sender_name,
                "to": [sender_name],
                "subject": "Local thread seed",
                "body_md": "local-only routing seed",
            },
        )
        arguments["message_id"] = seed.data["deliveries"][0]["payload"]["id"]
    return await client.call_tool(tool, arguments, raise_on_error=False)


async def _assert_routing_refused(
    result, body: str, *, ambiguous: bool = False, error_type: str | None = None
) -> None:
    assert result.is_error or (isinstance(result.data, dict) and result.data.get("error")), result
    if ambiguous:
        assert "matches multiple agents" in str(result)
    if error_type is not None:
        # MCP transports may expose only the error message, while direct tool
        # results retain the structured error code. Both must name the policy
        # refusal; a routing-not-found error is not sufficient evidence.
        policy_messages = {
            "CONTACT_BLOCKED": "Recipient is not accepting messages",
            "CONTACT_REQUIRED": "Contact approval required",
        }
        assert error_type in str(result) or policy_messages[error_type] in str(result)
    async with get_session() as session:
        rows = await session.execute(text("SELECT id FROM messages WHERE body_md = :body"), {"body": body})
        assert not rows.all(), "a refused route must not persist or partially deliver the message"


@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.parametrize(
    ("sender_name", "recipient_name"),
    [
        ("BlueLake", "GreenCastle"),
        ("Alice.von.Client.ProjA", "Bob.von.Client.ProjB"),
        ("alice-one", "bob-two"),
        ("alice_one", "bob_two"),
    ],
)
@pytest.mark.asyncio
async def test_cross_project_punctuation_send_and_reply(
    routing_env, address_style, sender_name, recipient_name
):
    """Issue #281: every supported identity survives send and reply routing."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", sender_name)
        recipient = await _register_routing_agent(client, "/routing-b", recipient_name)
        await _approve_routing_contact(client, "/routing-a", sender_name, "/routing-b", recipient_name)
        await _approve_routing_contact(client, "/routing-b", recipient_name, "/routing-a", sender_name)
        recipient_spelling = recipient_name if recipient_name.isalnum() else recipient_name.lower()
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "/routing-a",
                "sender_name": sender_name,
                "to": [_routing_address(address_style, "/routing-b", recipient_spelling)],
                "subject": "Punctuation routing",
                "body_md": "punctuation outgoing",
            },
        )
        assert [d["project"] for d in sent.data["deliveries"]] == ["/routing-b"]
        message_id = sent.data["deliveries"][0]["payload"]["id"]
        await _assert_message_identity("punctuation outgoing", sender["id"], recipient["id"])

        for explicit in (False, True):
            body = f"punctuation reply explicit={explicit}"
            arguments: dict[str, Any] = {
                "project_key": "/routing-b",
                "sender_name": recipient_name,
                "message_id": message_id,
                "body_md": body,
            }
            if explicit:
                arguments["to"] = [_routing_address(address_style, "/routing-a", sender_name.lower())]
            reply = await client.call_tool("reply_message", arguments)
            assert [d["project"] for d in reply.data["deliveries"]] == ["/routing-a"]
            await _assert_message_identity(body, recipient["id"], sender["id"])

        storage = Path(get_settings().storage.root)
        archived = list((storage / "projects" / "routing-b" / "messages").rglob("*.md"))
        assert any("punctuation outgoing" in path.read_text() for path in archived)


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.asyncio
async def test_cross_project_punctuation_requires_active_approved_contact(routing_env, tool, address_style):
    """Missing, pending, blocked, and expired links cannot authorize a route."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        recipient = await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        address = _routing_address(address_style, "/routing-b", "Blue.Lake")
        for status in ("missing", "pending", "blocked", "expired"):
            if status == "pending":
                await client.call_tool(
                    "request_contact",
                    {
                        "project_key": "/routing-a",
                        "from_agent": "GreenCastle",
                        "to_project": "/routing-b",
                        "to_agent": "Blue.Lake",
                    },
                )
            elif status in {"blocked", "expired"}:
                await client.call_tool(
                    "respond_contact",
                    {
                        "project_key": "/routing-b",
                        "to_agent": "Blue.Lake",
                        "from_project": "/routing-a",
                        "from_agent": "GreenCastle",
                        "accept": status == "expired",
                    },
                )
                if status == "expired":
                    expired = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
                    async with get_session() as session:
                        await session.execute(
                            text(
                                "UPDATE agent_links SET expires_ts = :expired "
                                "WHERE a_agent_id = :sender AND b_agent_id = :recipient"
                            ),
                            {
                                "expired": expired.strftime("%Y-%m-%d %H:%M:%S.%f"),
                                "sender": sender["id"],
                                "recipient": recipient["id"],
                            },
                        )
                        await session.commit()
            body = f"must reject {tool} with {status} link"
            result = await _routing_attempt(client, tool, address, body)
            await _assert_routing_refused(result, body)


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["project", "at"])
@pytest.mark.asyncio
async def test_cross_project_punctuation_link_is_scoped_to_sender_and_project(routing_env, tool, address_style):
    """Approval for another sender or a same-name agent in another project is insufficient."""
    async with Client(build_mcp_server()) as client:
        await _register_routing_agent(client, "/routing-a", "GreenCastle")
        await _register_routing_agent(client, "/routing-a", "RedStone")
        await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        await _register_routing_agent(client, "/routing-c", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "RedStone", "/routing-b", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-c", "Blue.Lake")
        body = "must reject approval for another identity or project"
        result = await _routing_attempt(
            client, tool, _routing_address(address_style, "/routing-b", "Blue.Lake"), body
        )
        await _assert_routing_refused(result, body)


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.parametrize("enforcement", [True, False])
@pytest.mark.asyncio
async def test_cross_project_punctuation_block_all_is_preserved(
    routing_env, monkeypatch, tool, address_style, enforcement
):
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", str(enforcement).lower())
    clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await _register_routing_agent(client, "/routing-a", "GreenCastle")
        await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-b", "Blue.Lake")
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-b", "agent_name": "Blue.Lake", "policy": "block_all"},
        )
        body = "must reject block_all regardless of approval or enforcement"
        result = await _routing_attempt(
            client, tool, _routing_address(address_style, "/routing-b", "Blue.Lake"), body
        )
        await _assert_routing_refused(result, body, error_type="CONTACT_BLOCKED")


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.asyncio
async def test_cross_project_exact_identity_precedes_approved_normalization_alias(routing_env, tool, address_style):
    """An approved stripped-name agent must never substitute for an exact dotted identity."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        normalized = await _register_routing_agent(client, "/routing-b", "BlueLake")
        exact = await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-b", "BlueLake")

        address = _routing_address(address_style, "/routing-b", "blue.lake")
        body = "unapproved exact identity must not become approved alias"
        rejected = await _routing_attempt(client, tool, address, body)
        await _assert_routing_refused(rejected, body)

        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-b", "Blue.Lake")
        body = "both approved must select exact dotted identity"
        accepted = await _routing_attempt(client, tool, address, body)
        assert not accepted.is_error
        await _assert_message_identity(body, sender["id"], exact["id"])

        body = "stripped spelling still selects its exact identity"
        accepted = await _routing_attempt(
            client, tool, _routing_address(address_style, "/routing-b", "BlueLake"), body
        )
        assert not accepted.is_error
        await _assert_message_identity(body, sender["id"], normalized["id"])

        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-b", "agent_name": "Blue.Lake", "policy": "block_all"},
        )
        body = "blocked exact identity must not become approved alias"
        rejected = await _routing_attempt(client, tool, address, body)
        await _assert_routing_refused(rejected, body, error_type="CONTACT_BLOCKED")


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.parametrize("approved_count", [1, 2])
@pytest.mark.asyncio
async def test_cross_project_normalization_collision_is_ambiguous(routing_env, tool, address_style, approved_count):
    async with Client(build_mcp_server()) as client:
        await _register_routing_agent(client, "/routing-a", "GreenCastle")
        for index, name in enumerate(("Blue.Lake", "Blue-Lake")):
            await _register_routing_agent(client, "/routing-b", name)
            if index < approved_count:
                await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-b", name)
        # Neither exact spelling exists. Both normalize to the same alias;
        # database row order and set iteration must not choose an identity.
        body = "must reject ambiguous normalized recipient"
        result = await _routing_attempt(
            client, tool, _routing_address(address_style, "/routing-b", "Blue_Lake"), body
        )
        await _assert_routing_refused(result, body, ambiguous=True)


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.parametrize("address_style", ["bare", "project", "at"])
@pytest.mark.asyncio
async def test_cross_project_unique_normalization_alias_preserves_identity(routing_env, tool, address_style):
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        recipient = await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-b", "Blue.Lake")
        body = "unique normalized alias resolves the stored dotted identity"
        result = await _routing_attempt(
            client, tool, _routing_address(address_style, "/routing-b", "BlueLake"), body
        )
        assert not result.is_error
        await _assert_message_identity(body, sender["id"], recipient["id"])


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.asyncio
async def test_cross_project_bare_name_ambiguity_requires_project(routing_env, tool):
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        recipients = {}
        for project in ("/routing-b", "/routing-c"):
            recipients[project] = await _register_routing_agent(client, project, "Blue.Lake")
            await _approve_routing_contact(client, "/routing-a", "GreenCastle", project, "Blue.Lake")
        body = "must reject ambiguous remote project"
        rejected = await _routing_attempt(client, tool, "Blue.Lake", body)
        await _assert_routing_refused(rejected, body, ambiguous=True)
        for style, project in (("project", "/routing-b"), ("at", "/routing-c")):
            body = f"explicit project selects {project}"
            accepted = await _routing_attempt(client, tool, _routing_address(style, project, "Blue.Lake"), body)
            assert not accepted.is_error
            await _assert_message_identity(body, sender["id"], recipients[project]["id"])


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.asyncio
async def test_cross_project_exact_recipient_is_not_a_normalized_self_send(routing_env, tool):
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "BlueLake")
        recipient = await _register_routing_agent(client, "/routing-b", "Blue.Lake")
        await _approve_routing_contact(client, "/routing-a", "BlueLake", "/routing-b", "Blue.Lake")
        body = "exact external dotted name must not resolve to sender"
        result = await _routing_attempt(client, tool, "Blue.Lake", body, sender_name="BlueLake")
        assert not result.is_error
        await _assert_message_identity(body, sender["id"], recipient["id"])


@pytest.mark.asyncio
async def test_cross_project_dotted_thread_reply_preserves_authorization_and_block_all(routing_env):
    """A visible external sender still requires approval in the reply direction."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "Alice.von.Client.ProjA")
        recipient = await _register_routing_agent(client, "/routing-b", "Bob.von.Client.ProjB")
        await _approve_routing_contact(
            client, "/routing-a", "Alice.von.Client.ProjA", "/routing-b", "Bob.von.Client.ProjB"
        )
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "/routing-a",
                "sender_name": sender["name"],
                "to": [recipient["name"]],
                "subject": "One-way approved link",
                "body_md": "original dotted sender",
            },
        )
        arguments = {
            "project_key": "/routing-b",
            "sender_name": recipient["name"],
            "message_id": sent.data["deliveries"][0]["payload"]["id"],
            "body_md": "visible original sender still requires reverse approval",
        }
        rejected = await client.call_tool("reply_message", arguments, raise_on_error=False)
        await _assert_routing_refused(rejected, arguments["body_md"])

        await _approve_routing_contact(
            client, "/routing-b", recipient["name"], "/routing-a", sender["name"]
        )
        arguments["body_md"] = "reverse approval authorizes the original sender"
        reply = await client.call_tool("reply_message", arguments)
        assert reply.data["deliveries"]
        await _assert_message_identity(arguments["body_md"], recipient["id"], sender["id"])

        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-a", "agent_name": sender["name"], "policy": "block_all"},
        )
        arguments["body_md"] = "thread visibility and approval must not bypass block_all"
        rejected = await client.call_tool("reply_message", arguments, raise_on_error=False)
        await _assert_routing_refused(rejected, arguments["body_md"], error_type="CONTACT_BLOCKED")


@pytest.mark.parametrize("sender_name", ["GreenCastle", "Alice.von.Client.ProjA"])
@pytest.mark.asyncio
async def test_cross_project_bare_reply_prefers_approved_remote_over_local_shadow(routing_env, sender_name):
    """Default and explicit bare replies retain the approved remote identity."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", sender_name)
        recipient = await _register_routing_agent(client, "/routing-b", "Bob.von.Client.ProjB")
        await _register_routing_agent(client, "/routing-b", sender_name)
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-b", "agent_name": sender_name, "policy": "open"},
        )
        await _approve_routing_contact(
            client, "/routing-a", sender_name, "/routing-b", recipient["name"]
        )
        await _approve_routing_contact(
            client, "/routing-b", recipient["name"], "/routing-a", sender_name
        )
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "/routing-a",
                "sender_name": sender_name,
                "to": [recipient["name"]],
                "subject": "Remote sender with a local shadow",
                "body_md": "remote sender seeds a reply thread",
            },
        )
        for explicit in (False, True):
            body = f"remote reply must bypass local shadow explicit={explicit}"
            arguments: dict[str, Any] = {
                "project_key": "/routing-b",
                "sender_name": recipient["name"],
                "message_id": sent.data["deliveries"][0]["payload"]["id"],
                "body_md": body,
            }
            if explicit:
                arguments["to"] = [sender_name]
            reply = await client.call_tool("reply_message", arguments)
            assert [delivery["project"] for delivery in reply.data["deliveries"]] == ["/routing-a"]
            await _assert_message_identity(body, recipient["id"], sender["id"])


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.asyncio
async def test_local_normalization_preserves_contact_policy(routing_env, tool):
    """Resolving a local alias must not skip the stored recipient's contact gate."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        recipient = await _register_routing_agent(client, "/routing-a", "Blue.Lake")
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-a", "agent_name": "Blue.Lake", "policy": "contacts_only"},
        )
        body = "local alias requires approved contact"
        rejected = await _routing_attempt(client, tool, "BlueLake", body)
        await _assert_routing_refused(rejected, body, error_type="CONTACT_REQUIRED")

        await _approve_routing_contact(client, "/routing-a", "GreenCastle", "/routing-a", "Blue.Lake")
        body = "approved local alias retains exact recipient"
        accepted = await _routing_attempt(client, tool, "BlueLake", body)
        assert not accepted.is_error
        await _assert_message_identity(body, sender["id"], recipient["id"])

        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-a", "agent_name": "Blue.Lake", "policy": "block_all"},
        )
        body = "local alias cannot bypass block_all"
        rejected = await _routing_attempt(client, tool, "BlueLake", body)
        await _assert_routing_refused(rejected, body, error_type="CONTACT_BLOCKED")


@pytest.mark.parametrize("recipient_name", ["BlueLake", "Blue.Lake"])
@pytest.mark.asyncio
async def test_cross_project_recipient_survives_local_auto_approval_retry(routing_env, recipient_name):
    """Retrying a local contact approval must not recheck an unrelated local shadow."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        shadow = await _register_routing_agent(client, "/routing-a", recipient_name)
        local_recipient = await _register_routing_agent(client, "/routing-a", "RedStone")
        remote_recipient = await _register_routing_agent(client, "/routing-b", recipient_name)
        for name in (recipient_name, "RedStone"):
            await client.call_tool(
                "set_contact_policy",
                {"project_key": "/routing-a", "agent_name": name, "policy": "contacts_only"},
            )
        await _approve_routing_contact(
            client, "/routing-a", sender["name"], "/routing-b", recipient_name
        )
        body = "mixed local and remote delivery survives local approval retry"
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/routing-a",
                "sender_name": sender["name"],
                "to": [recipient_name, local_recipient["name"]],
                "subject": "Mixed recipient contact approval",
                "body_md": body,
                "auto_contact_if_blocked": True,
            },
        )
        assert sorted(delivery["project"] for delivery in result.data["deliveries"]) == [
            "/routing-a", "/routing-b"
        ]
        async with get_session() as session:
            rows = await session.execute(
                text(
                    "SELECT m.sender_id, r.agent_id, m.project_id = a.project_id "
                    "FROM messages m JOIN message_recipients r ON r.message_id = m.id "
                    "JOIN agents a ON a.id = r.agent_id WHERE m.body_md = :body"
                ),
                {"body": body},
            )
            assert sorted(tuple(row) for row in rows) == sorted([
                (sender["id"], local_recipient["id"], 1),
                (sender["id"], remote_recipient["id"], 1),
            ])
            shadow_links = await session.execute(
                text("SELECT id FROM agent_links WHERE a_agent_id = :sender AND b_agent_id = :shadow"),
                {"sender": sender["id"], "shadow": shadow["id"]},
            )
            assert not shadow_links.all(), "the local shadow must not receive an unintended contact approval"


@pytest.mark.parametrize("tool", ["send_message", "reply_message"])
@pytest.mark.asyncio
async def test_local_exact_identity_cannot_borrow_remote_normalized_approval(routing_env, tool):
    """An approved external alias must not bypass the exact local recipient's policy."""
    async with Client(build_mcp_server()) as client:
        sender = await _register_routing_agent(client, "/routing-a", "GreenCastle")
        local_recipient = await _register_routing_agent(client, "/routing-a", "Blue.Lake")
        await _register_routing_agent(client, "/routing-b", "BlueLake")
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "/routing-a", "agent_name": local_recipient["name"], "policy": "contacts_only"},
        )
        await _approve_routing_contact(client, "/routing-a", sender["name"], "/routing-b", "BlueLake")
        body = "approved external alias must not authorize an exact local recipient"
        rejected = await _routing_attempt(client, tool, local_recipient["name"], body)
        await _assert_routing_refused(rejected, body, error_type="CONTACT_REQUIRED")

        await _approve_routing_contact(
            client, "/routing-a", sender["name"], "/routing-a", local_recipient["name"]
        )
        body = "the exact local identity is reachable after its own approval"
        accepted = await _routing_attempt(client, tool, local_recipient["name"], body)
        assert not accepted.is_error
        await _assert_message_identity(body, sender["id"], local_recipient["id"])


async def _seed_punctuated_contacts(sender_name, recipient_name, status="approved", collision=None):
    await _seed_two_projects_no_link()
    async with get_session() as session:
        sender = (await session.execute(select(Agent).where(cast(Any, Agent.name == "RedFalcon")))).scalar_one()
        recipient = (await session.execute(select(Agent).where(cast(Any, Agent.name == "GoldHarbor")))).scalar_one()
        sender.name = sender_name
        recipient.name = recipient_name
        session.add_all([sender, recipient])
        session.add_all([
            AgentLink(a_project_id=sender.project_id, a_agent_id=sender.id,
                      b_project_id=recipient.project_id, b_agent_id=recipient.id, status=status),
            AgentLink(a_project_id=recipient.project_id, a_agent_id=recipient.id,
                      b_project_id=sender.project_id, b_agent_id=sender.id, status=status),
        ])
        if collision:
            session.add(Agent(project_id=recipient.project_id, name=collision, program="codex", model="gpt-5",
                              task_description="", registration_token="collision-token"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("Alice.von.A", "Bob.von.B"), ("Alice-A", "Bob_B"), ("Alice", "Bob")])
@pytest.mark.parametrize("address", ["{}", "project:frontend#{}", "{}@frontend"])
async def test_punctuated_cross_project_send_and_reply(isolated_env, names, address):
    sender, recipient = names
    await _seed_punctuated_contacts(sender, recipient)
    async with Client(build_mcp_server()) as client:
        sent = await client.call_tool("send_message", {
            "project_key": "Backend", "sender_name": sender, "sender_token": "redfalcon-token",
            "to": [address.format(recipient)], "subject": "punctuation", "body_md": "hello",
        })
        delivery = sent.data["deliveries"][0]
        assert delivery["project"] == "Frontend"
        assert delivery["payload"]["to"] == [recipient]
        for reply_to in (None, [sender], [f"{sender}@backend"]):
            args = {"project_key": "Frontend", "sender_name": recipient, "sender_token": "goldharbor-token",
                    "message_id": delivery["payload"]["id"], "body_md": "reply"}
            if reply_to is not None:
                args["to"] = reply_to
            reply = await client.call_tool("reply_message", args)
            assert reply.data["deliveries"][0]["project"] == "Backend"
            assert reply.data["deliveries"][0]["payload"]["to"] == [sender]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "blocked"])
async def test_punctuated_unapproved_contact_is_rejected(isolated_env, status):
    await _seed_punctuated_contacts("Alice.von.A", "Bob.von.B", status=status)
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool("send_message", {
            "project_key": "Backend", "sender_name": "Alice.von.A", "sender_token": "redfalcon-token",
            "to": ["Bob.von.B@frontend"], "subject": "blocked", "body_md": "hello",
        }, raise_on_error=False)
        assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["Bob-vonB", "project:frontend#Bob-vonB"])
async def test_ambiguous_normalization_cannot_select_approved_identity(isolated_env, address):
    await _seed_punctuated_contacts("Alice", "Bob.von.B", collision="BobvonB")
    async with Client(build_mcp_server()) as client:
        for target in (address, "BobvonB@frontend"):
            result = await client.call_tool("send_message", {
                "project_key": "Backend", "sender_name": "Alice", "sender_token": "redfalcon-token",
                "to": [target], "subject": "collision", "body_md": "must fail",
            }, raise_on_error=False)
            assert result.is_error
        # An exact dotted name still identifies its own approved agent.
        result = await client.call_tool("send_message", {
            "project_key": "Backend", "sender_name": "Alice", "sender_token": "redfalcon-token",
            "to": ["Bob.von.B@frontend"], "subject": "exact", "body_md": "hello",
        })
        assert result.data["deliveries"][0]["payload"]["to"] == ["Bob.von.B"]
        seed = await client.call_tool("send_message", {
            "project_key": "Frontend", "sender_name": "Bob.von.B", "sender_token": "goldharbor-token",
            "to": ["Alice@backend"], "subject": "seed reply", "body_md": "hello",
        })
        reply = await client.call_tool("reply_message", {
            "project_key": "Backend", "sender_name": "Alice", "sender_token": "redfalcon-token",
            "message_id": seed.data["deliveries"][0]["payload"]["id"], "to": [address], "body_md": "ambiguous",
        }, raise_on_error=False)
        assert reply.is_error or reply.data.get("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["pending", "expired", "block_all", "wrong_project"])
async def test_punctuated_reply_cannot_bypass_contact_restrictions(isolated_env, restriction):
    from datetime import datetime, timedelta, timezone

    await _seed_punctuated_contacts("Alice.von.A", "Bob.von.B")
    async with Client(build_mcp_server()) as client:
        sent = await client.call_tool("send_message", {
            "project_key": "Backend", "sender_name": "Alice.von.A", "sender_token": "redfalcon-token",
            "to": ["Bob.von.B"], "subject": "seed", "body_md": "hello",
        })
        async with get_session() as session:
            alice = (await session.execute(select(Agent).where(
                cast(Any, Agent.name == "Alice.von.A"),
            ))).scalar_one()
            link = (await session.execute(select(AgentLink).where(
                cast(Any, AgentLink.b_agent_id == alice.id),
            ))).scalar_one()
            if restriction == "pending":
                link.status = "pending"
            elif restriction == "expired":
                link.expires_ts = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif restriction == "wrong_project":
                link.b_project_id = link.a_project_id
            else:
                alice.contact_policy = "block_all"
            session.add_all([alice, link])
            await session.commit()
        for tool in ("send_message", "reply_message"):
            args = {"project_key": "Frontend", "sender_name": "Bob.von.B",
                    "sender_token": "goldharbor-token", "body_md": "must fail"}
            if tool == "reply_message":
                args["message_id"] = sent.data["deliveries"][0]["payload"]["id"]
            else:
                args.update(to=["Alice.von.A@backend"], subject="blocked", auto_contact_if_blocked=False)
            result = await client.call_tool(tool, args, raise_on_error=False)
            assert result.is_error or result.data.get("error")


async def _seed_two_projects_no_link() -> None:
    """Create Backend/Frontend projects with one agent each and NO AgentLink."""
    await ensure_schema()
    async with get_session() as s:
        backend = Project(slug="backend", human_key="Backend")
        frontend = Project(slug="frontend", human_key="Frontend")
        s.add_all([backend, frontend])
        await s.commit()
        await s.refresh(backend)
        await s.refresh(frontend)
        assert backend.id is not None and frontend.id is not None
        s.add_all(
            [
                Agent(
                    project_id=backend.id,
                    name="RedFalcon",
                    program="codex",
                    model="gpt-5",
                    task_description="",
                    registration_token="redfalcon-token",
                ),
                Agent(
                    project_id=frontend.id,
                    name="GoldHarbor",
                    program="codex",
                    model="gpt-5",
                    task_description="",
                    registration_token="goldharbor-token",
                ),
            ]
        )
        await s.commit()


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
async def test_cross_project_send_requires_link_by_default(isolated_env):
    """With enforcement on (default), explicit cross-project sends still need a link."""
    await _seed_two_projects_no_link()
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "RedFalcon",
                "sender_token": "redfalcon-token",
                "to": ["project:frontend#GoldHarbor"],
                "subject": "Cross",
                "body_md": "hello",
            },
            raise_on_error=False,
        )
        deliveries = [] if res.is_error else (res.data.get("deliveries") or [])
        assert not any(d.get("project") == "Frontend" for d in deliveries), (
            "cross-project delivery must not succeed without an approved link "
            "while CONTACT_ENFORCEMENT_ENABLED=true"
        )


@pytest.mark.asyncio
async def test_cross_project_send_and_reply_without_link_when_enforcement_disabled(
    isolated_env, monkeypatch
):
    """CONTACT_ENFORCEMENT_ENABLED=false relaxes the cross-project link requirement.

    Regression for issue #257: with enforcement disabled, explicitly addressed
    cross-project recipients should be deliverable without an AgentLink
    handshake, for both send_message and reply_message.
    """
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "false")
    clear_settings_cache()
    try:
        await _seed_two_projects_no_link()
        server = build_mcp_server()
        async with Client(server) as client:
            sent = await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "RedFalcon",
                    "sender_token": "redfalcon-token",
                    "to": ["project:frontend#GoldHarbor"],
                    "subject": "Cross",
                    "body_md": "hello",
                },
            )
            deliveries = sent.data.get("deliveries") or []
            assert any(d.get("project") == "Frontend" for d in deliveries)
            message_id = deliveries[0]["payload"]["id"]

            reply = await client.call_tool(
                "reply_message",
                {
                    "project_key": "Frontend",
                    "message_id": message_id,
                    "sender_name": "GoldHarbor",
                    "sender_token": "goldharbor-token",
                    "body_md": "ack",
                },
            )
            reply_deliveries = reply.data.get("deliveries") or []
            assert any(d.get("project") == "Backend" for d in reply_deliveries)
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_cross_project_send_enforcement_disabled_still_honors_block_all(
    isolated_env, monkeypatch
):
    """Even with enforcement disabled, block_all recipients stay unreachable."""
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "false")
    clear_settings_cache()
    try:
        await _seed_two_projects_no_link()
        async with get_session() as s:
            from sqlalchemy import select as _select

            agent = (
                await s.execute(_select(Agent).where(col(Agent.name) == "GoldHarbor"))
            ).scalars().first()
            assert agent is not None
            agent.contact_policy = "block_all"
            s.add(agent)
            await s.commit()

        server = build_mcp_server()
        async with Client(server) as client:
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "RedFalcon",
                    "sender_token": "redfalcon-token",
                    "to": ["project:frontend#GoldHarbor"],
                    "subject": "Cross",
                    "body_md": "hello",
                },
                raise_on_error=False,
            )
            assert res.is_error, "block_all recipient must remain blocked"
    finally:
        clear_settings_cache()


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
