"""Tests for the `unread_only` filter on `fetch_inbox` and `fetch_topic`.

Covers:
- Regression guard: omitting/`False` returns the same set as before (no behavior change).
- Basic filter: `True` returns only messages whose recipient row has `read_ts IS NULL`.
- Per-recipient semantics: a message read by Agent A is still unread for Agent B.
- AND-composition: combines correctly with `since_ts` and `topic` filters.
- `fetch_topic`: same filter applies to topic-tagged mail per viewer.

The unread definition matches the rest of the server: "never explicitly marked
read via `mark_message_read` or `acknowledge_message`." A bare fetch does NOT
mark read.
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

logger = logging.getLogger(__name__)


def _get_data(result):
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
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


async def _setup(client, project_key: str, agent_names: list[str]) -> None:
    """Register a project with N named agents."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    for name in agent_names:
        await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-prog",
                "model": "test-model",
                "name": name,
            },
        )


# ============================================================================
# fetch_inbox: unread_only filter
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_false_is_regression_safe(isolated_env):
    """Default behavior preserved: `unread_only=False` returns the same set as omitting it."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-regression"
        await _setup(client, project, ["GreenCastle", "RedStone"])

        # Send 3 messages, mark one read.
        sent_ids = []
        for i in range(3):
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["RedStone"],
                    "subject": f"msg-{i}",
                    "body_md": "x",
                },
            )
            sent_ids.append(int(_get_data(res)["deliveries"][0]["payload"]["id"]))
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": sent_ids[0]},
        )

        without_flag = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "RedStone"},
            )
        )
        explicit_false = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "RedStone", "unread_only": False},
            )
        )

        assert {m["id"] for m in without_flag} == {m["id"] for m in explicit_false}
        assert {m["id"] for m in without_flag} == set(sent_ids)


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_true_filters_to_unread(isolated_env):
    """`unread_only=True` returns only messages where this recipient's `read_ts IS NULL`."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-basic"
        await _setup(client, project, ["GreenCastle", "RedStone"])

        # Send 3 messages.
        sent_ids = []
        for i in range(3):
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["RedStone"],
                    "subject": f"msg-{i}",
                    "body_md": "x",
                },
            )
            sent_ids.append(int(_get_data(res)["deliveries"][0]["payload"]["id"]))

        # Mark first explicitly read; ack second (which sets read_ts internally).
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": sent_ids[0]},
        )
        await client.call_tool(
            "acknowledge_message",
            {"project_key": project, "agent_name": "RedStone", "message_id": sent_ids[1]},
        )

        unread = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "RedStone", "unread_only": True},
            )
        )

        unread_ids = {m["id"] for m in unread}
        assert unread_ids == {sent_ids[2]}, f"expected only msg-2 unread, got {unread_ids}"


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_is_per_recipient_not_per_message(isolated_env):
    """A message read by one recipient must still appear unread for another recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-per-recipient"
        await _setup(client, project, ["GreenCastle", "RedStone", "BlueLake"])

        # One message addressed to both RedStone and BlueLake.
        res = await client.call_tool(
            "send_message",
            {
                "project_key": project,
                "sender_name": "GreenCastle",
                "to": ["RedStone", "BlueLake"],
                "subject": "shared",
                "body_md": "x",
            },
        )
        msg_id = int(_get_data(res)["deliveries"][0]["payload"]["id"])

        # RedStone reads it; BlueLake does not.
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": msg_id},
        )

        red_unread = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "RedStone", "unread_only": True},
            )
        )
        blue_unread = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "BlueLake", "unread_only": True},
            )
        )

        assert msg_id not in {m["id"] for m in red_unread}, "RedStone marked it read; should be filtered out for them"
        assert msg_id in {m["id"] for m in blue_unread}, "BlueLake never read it; should still appear unread for them"


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_combines_with_since_ts_and_topic(isolated_env):
    """`unread_only` ANDs with `since_ts` and `topic` — all filters must hold."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-combined"
        await _setup(client, project, ["GreenCastle", "RedStone"])

        # Send four messages with different topics; capture timestamps.
        ids: dict[str, int] = {}
        timestamps: dict[str, str] = {}
        for label, topic in [("a", "alpha"), ("b", "alpha"), ("c", "beta"), ("d", "beta")]:
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["RedStone"],
                    "subject": label,
                    "body_md": "x",
                    "topic": topic,
                },
            )
            payload = _get_data(res)["deliveries"][0]["payload"]
            ids[label] = int(payload["id"])
            timestamps[label] = payload["created_ts"]

        # Mark "a" read; topic=alpha; "b" still unread on alpha.
        # Mark "c" read; topic=beta;  "d" still unread on beta.
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": ids["a"]},
        )
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": ids["c"]},
        )

        # Filter: unread_only + topic=alpha → only "b".
        alpha_unread = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": project,
                    "agent_name": "RedStone",
                    "unread_only": True,
                    "topic": "alpha",
                },
            )
        )
        assert {m["id"] for m in alpha_unread} == {ids["b"]}

        # Filter: unread_only + since_ts=after(b) → only "d" (only unread strictly newer than b).
        after_b_unread = _get_list(
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": project,
                    "agent_name": "RedStone",
                    "unread_only": True,
                    "since_ts": timestamps["b"],
                },
            )
        )
        assert {m["id"] for m in after_b_unread} == {ids["d"]}


# ============================================================================
# fetch_topic: unread_only filter
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_topic_unread_only_filters_per_viewer(isolated_env):
    """`fetch_topic` with `unread_only=True` returns only topic messages where the viewer has unread."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-topic"
        await _setup(client, project, ["GreenCastle", "RedStone"])

        # Two messages, same topic, both addressed to RedStone.
        ids: list[int] = []
        for label in ["m1", "m2"]:
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["RedStone"],
                    "subject": label,
                    "body_md": "x",
                    "topic": "standup",
                },
            )
            ids.append(int(_get_data(res)["deliveries"][0]["payload"]["id"]))

        # RedStone reads m1.
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": ids[0]},
        )

        # Without flag: both messages.
        all_topic = _get_list(
            await client.call_tool(
                "fetch_topic",
                {
                    "project_key": project,
                    "topic_name": "standup",
                    "agent_name": "RedStone",
                },
            )
        )
        assert {m["id"] for m in all_topic} == set(ids)

        # With flag: only m2 unread for RedStone.
        only_unread = _get_list(
            await client.call_tool(
                "fetch_topic",
                {
                    "project_key": project,
                    "topic_name": "standup",
                    "agent_name": "RedStone",
                    "unread_only": True,
                },
            )
        )
        assert {m["id"] for m in only_unread} == {ids[1]}


@pytest.mark.asyncio
async def test_fetch_topic_unread_only_combines_with_since_ts(isolated_env):
    """`fetch_topic` ANDs `unread_only` with `since_ts` (mirrors the inbox combined test)."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = "/test/unread-topic-since"
        await _setup(client, project, ["GreenCastle", "RedStone"])

        ids: list[int] = []
        timestamps: list[str] = []
        for label in ["m1", "m2", "m3"]:
            res = await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["RedStone"],
                    "subject": label,
                    "body_md": "x",
                    "topic": "release",
                },
            )
            payload = _get_data(res)["deliveries"][0]["payload"]
            ids.append(int(payload["id"]))
            timestamps.append(payload["created_ts"])

        # All three unread; since_ts > m1 should narrow to {m2, m3}.
        # Then mark m2 read; with unread_only=True the result becomes {m3} only.
        await client.call_tool(
            "mark_message_read",
            {"project_key": project, "agent_name": "RedStone", "message_id": ids[1]},
        )

        result = _get_list(
            await client.call_tool(
                "fetch_topic",
                {
                    "project_key": project,
                    "topic_name": "release",
                    "agent_name": "RedStone",
                    "since_ts": timestamps[0],
                    "unread_only": True,
                },
            )
        )
        assert {m["id"] for m in result} == {ids[2]}
