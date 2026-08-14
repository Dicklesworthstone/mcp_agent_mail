"""Regression tests for issue #255: authenticated mail operations refresh
``last_active_ts``.

Mail-only agents (long-lived orchestrator identities that mostly send, fetch,
and acknowledge mail) must not go stale in discovery while they are actively
communicating. Sending already bumps the sender inside ``_create_message``;
``_touch_agent_activity`` (called from the shared authentication path) covers
every other authenticated operation — fetch_inbox, mark_message_read,
acknowledge_message, and so on.

Also covers the issue #254 sibling guarantee on this implementation: a bare
``fetch_inbox`` never mutates per-recipient read state, and inbox rows expose
``read_at`` so the (read-inclusive) default view stays distinguishable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Agent


def _naive_utc(when: datetime | None = None) -> datetime:
    target = when or datetime.now(timezone.utc)
    if target.tzinfo is not None:
        target = target.astimezone(timezone.utc).replace(tzinfo=None)
    return target


def _list(result):
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


def _data(result):
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, dict):
            return sc
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return getattr(result, "data", result)


async def _register(client, project_key: str, name: str) -> str:
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "claude-code",
            "model": "opus-4",
            "name": name,
            "task_description": "activity touch test",
        },
    )
    return _data(result)["name"]


async def _backdate(agent_name: str, hours: int) -> datetime:
    """Set the agent's last_active_ts `hours` in the past; return that value."""
    stamp = _naive_utc(datetime.now(timezone.utc) - timedelta(hours=hours))
    async with get_session() as session:
        await session.execute(
            Agent.__table__.update()
            .where(Agent.name == agent_name)
            .values(last_active_ts=stamp)
        )
        await session.commit()
    return stamp


async def _last_active(agent_name: str) -> datetime:
    async with get_session() as session:
        row = (
            await session.execute(
                Agent.__table__.select().where(Agent.name == agent_name)
            )
        ).first()
        assert row is not None
        return row.last_active_ts


@pytest.mark.asyncio
async def test_fetch_inbox_refreshes_last_active_ts(isolated_env):
    """A mail-only agent that just polls its inbox must not look stale."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/activity-touch-fetch"
        await client.call_tool("ensure_project", {"human_key": proj})
        sender = await _register(client, proj, "TouchSender")
        recipient = await _register(client, proj, "TouchRecipient")

        await client.call_tool(
            "send_message",
            {
                "project_key": proj,
                "sender_name": sender,
                "to": [recipient],
                "subject": "s",
                "body_md": "b",
            },
        )

        backdated = await _backdate(recipient, hours=96)
        assert (await _last_active(recipient)) == backdated

        await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": recipient}
        )

        refreshed = await _last_active(recipient)
        assert refreshed > backdated
        assert (_naive_utc() - refreshed).total_seconds() < 300


@pytest.mark.asyncio
async def test_acknowledge_refreshes_last_active_ts(isolated_env):
    """acknowledge_message counts as activity for the acknowledging agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/activity-touch-ack"
        await client.call_tool("ensure_project", {"human_key": proj})
        sender = await _register(client, proj, "AckSender")
        recipient = await _register(client, proj, "AckRecipient")

        send_res = await client.call_tool(
            "send_message",
            {
                "project_key": proj,
                "sender_name": sender,
                "to": [recipient],
                "subject": "s",
                "body_md": "b",
                "ack_required": True,
            },
        )
        mid = int(_data(send_res)["deliveries"][0]["payload"]["id"])

        backdated = await _backdate(recipient, hours=96)

        await client.call_tool(
            "acknowledge_message",
            {"project_key": proj, "agent_name": recipient, "message_id": mid},
        )

        refreshed = await _last_active(recipient)
        assert refreshed > backdated


@pytest.mark.asyncio
async def test_send_message_refreshes_sender_last_active_ts(isolated_env):
    """Sending mail bumps the sender (the core #255 claim, already true via
    _create_message — locked in here as a regression guard)."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/activity-touch-send"
        await client.call_tool("ensure_project", {"human_key": proj})
        sender = await _register(client, proj, "SendTouch")
        recipient = await _register(client, proj, "SendTouchPeer")

        backdated = await _backdate(sender, hours=96)

        await client.call_tool(
            "send_message",
            {
                "project_key": proj,
                "sender_name": sender,
                "to": [recipient],
                "subject": "s",
                "body_md": "b",
            },
        )

        refreshed = await _last_active(sender)
        assert refreshed > backdated


@pytest.mark.asyncio
async def test_bare_fetch_does_not_mark_read_and_exposes_read_state(isolated_env):
    """Issue #254 semantics on this implementation: fetching never consumes
    the inbox, the default view includes read mail, and each row carries
    `read_at` so read vs unread stays distinguishable."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/fetch-non-mutating"
        await client.call_tool("ensure_project", {"human_key": proj})
        sender = await _register(client, proj, "PeekSender")
        recipient = await _register(client, proj, "PeekRecipient")

        send_res = await client.call_tool(
            "send_message",
            {
                "project_key": proj,
                "sender_name": sender,
                "to": [recipient],
                "subject": "s",
                "body_md": "b",
            },
        )
        mid = int(_data(send_res)["deliveries"][0]["payload"]["id"])

        # Two bare fetches: rows stay unread (read_at null) both times.
        for _ in range(2):
            rows = _list(
                await client.call_tool(
                    "fetch_inbox", {"project_key": proj, "agent_name": recipient}
                )
            )
            assert [m["id"] for m in rows] == [mid]
            assert rows[0]["read_at"] is None

        # Still unread through the unread_only lens after those fetches.
        unread = _list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": proj, "agent_name": recipient, "unread_only": True},
            )
        )
        assert [m["id"] for m in unread] == [mid]

        # Explicit mark-read: default view still shows the row, now stamped.
        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": recipient, "message_id": mid},
        )
        rows = _list(
            await client.call_tool(
                "fetch_inbox", {"project_key": proj, "agent_name": recipient}
            )
        )
        assert [m["id"] for m in rows] == [mid]
        assert rows[0]["read_at"] is not None
