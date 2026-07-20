"""Regression tests for issue #149: auto-retire stale agents.

Long-running multi-agent projects accumulate "active" agents whose
sessions ended without an explicit `retire_agent` call. After ~30+
of these, every new agent broadcast triggers contact_approval for
all of them and silently fails delivery. The `sweep_stale_agents`
helper retires agents whose `last_active_ts` is older than a
caller-configurable threshold so the contact-wall stops piling up.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastmcp import Client
from sqlalchemy import select as sa_select, update as sa_update

from mcp_agent_mail.app import build_mcp_server, sweep_stale_agents
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Agent, FileReservation, Project


def _naive_utc(when: datetime | None = None) -> datetime:
    target = when or datetime.now(timezone.utc)
    if target.tzinfo is not None:
        target = target.astimezone(timezone.utc).replace(tzinfo=None)
    return target


@pytest.mark.asyncio
async def test_sweep_retires_only_agents_past_threshold(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/staleagents"})

        stale_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "Staleagents",
                "program": "claude-code",
                "model": "opus-4",
                "name": "DustyMountain",
                "task_description": "Long-since-quiet agent",
            },
        )
        active_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "Staleagents",
                "program": "claude-code",
                "model": "opus-4",
                "name": "BrightForest",
                "task_description": "Currently working agent",
            },
        )

        stale_name = stale_result.data["name"]
        active_name = active_result.data["name"]

        # Backdate the stale agent's last_active_ts to two days ago.
        async with get_session() as session:
            stale_agent = (
                await session.execute(
                    Agent.__table__.select().where(Agent.name == stale_name)
                )
            ).first()
            assert stale_agent is not None
            stale_id = stale_agent.id
            two_days_ago = _naive_utc(
                datetime.now(timezone.utc) - timedelta(hours=48)
            )
            await session.execute(
                Agent.__table__.update()
                .where(Agent.id == stale_id)
                .values(last_active_ts=two_days_ago, retired_at=None)
            )
            await session.commit()

        # 24h threshold: stale agent retires, active agent stays.
        retired = await sweep_stale_agents(threshold_seconds=86400)
        assert [entry["agent_name"] for entry in retired] == [stale_name]

        async with get_session() as session:
            stale_after = (
                await session.execute(
                    Agent.__table__.select().where(Agent.name == stale_name)
                )
            ).first()
            active_after = (
                await session.execute(
                    Agent.__table__.select().where(Agent.name == active_name)
                )
            ).first()
            assert stale_after is not None
            assert active_after is not None
            assert stale_after.retired_at is not None
            assert active_after.retired_at is None


@pytest.mark.asyncio
async def test_sweep_is_idempotent(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/staleidempo"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "Staleidempo",
                "program": "claude-code",
                "model": "opus-4",
                "name": "QuietRiver",
                "task_description": "Will be backdated",
            },
        )
        target_name = result.data["name"]

        async with get_session() as session:
            target_id = (
                (
                    await session.execute(
                        Agent.__table__.select().where(Agent.name == target_name)
                    )
                )
                .first()
                .id
            )
            two_days_ago = _naive_utc(
                datetime.now(timezone.utc) - timedelta(hours=48)
            )
            await session.execute(
                Agent.__table__.update()
                .where(Agent.id == target_id)
                .values(last_active_ts=two_days_ago, retired_at=None)
            )
            await session.commit()

        first = await sweep_stale_agents(threshold_seconds=86400)
        assert len(first) == 1
        # Second pass must not re-retire the same agent.
        second = await sweep_stale_agents(threshold_seconds=86400)
        assert second == []


@pytest.mark.asyncio
async def test_sweep_threshold_floor(isolated_env):
    """Threshold values below 60s are clamped — sweep must still execute."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/stalefloor"})

        await client.call_tool(
            "register_agent",
            {
                "project_key": "Stalefloor",
                "program": "claude-code",
                "model": "opus-4",
                "name": "FuzzyCloud",
                "task_description": "Just registered",
            },
        )

        # last_active_ts was set just now; even a 0s argument is clamped to 60s,
        # so the just-registered agent must still NOT retire.
        retired = await sweep_stale_agents(threshold_seconds=0)
        assert retired == []


@pytest.mark.asyncio
async def test_on_demand_sweep_is_project_scoped_and_never_retires_caller(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/sweep/project-a"})
        await client.call_tool("ensure_project", {"human_key": "/sweep/project-b"})
        caller = await client.call_tool(
            "register_agent",
            {
                "project_key": "/sweep/project-a",
                "program": "codex",
                "model": "gpt-5",
                "name": "BrightRiver",
            },
        )
        stale_local = await client.call_tool(
            "register_agent",
            {
                "project_key": "/sweep/project-a",
                "program": "codex",
                "model": "gpt-5",
                "name": "QuietMountain",
            },
        )
        stale_other = await client.call_tool(
            "register_agent",
            {
                "project_key": "/sweep/project-b",
                "program": "codex",
                "model": "gpt-5",
                "name": "DustyForest",
            },
        )

        old = _naive_utc(datetime.now(timezone.utc) - timedelta(days=2))
        names = [caller.data["name"], stale_local.data["name"], stale_other.data["name"]]
        async with get_session() as session:
            await session.execute(
                sa_update(Agent)
                .where(cast(Any, Agent.name).in_(names))
                .values(last_active_ts=old, retired_at=None)
            )
            await session.commit()

        result = await client.call_tool(
            "sweep_stale_agents",
            {
                "project_key": "/sweep/project-a",
                "agent_name": caller.data["name"],
                "registration_token": caller.data["registration_token"],
                "threshold_seconds": 0,
            },
        )

        assert result.data["threshold_seconds"] == 60
        assert result.data["retired"] == [stale_local.data["name"]]
        assert result.data["count"] == 1
        async with get_session() as session:
            rows = (
                await session.execute(
                    sa_select(Agent).where(cast(Any, Agent.name).in_(names))
                )
            ).scalars().all()
        retired_by_name = {row.name: row.retired_at for row in rows}
        assert retired_by_name[caller.data["name"]] is None
        assert retired_by_name[stale_local.data["name"]] is not None
        assert retired_by_name[stale_other.data["name"]] is None


@pytest.mark.asyncio
async def test_on_demand_sweep_skips_unexpired_reservations_by_default(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        project_result = await client.call_tool("ensure_project", {"human_key": "/sweep/reservations"})
        caller = await client.call_tool(
            "register_agent",
            {
                "project_key": "/sweep/reservations",
                "program": "codex",
                "model": "gpt-5",
                "name": "SilverLake",
            },
        )
        stale = await client.call_tool(
            "register_agent",
            {
                "project_key": "/sweep/reservations",
                "program": "codex",
                "model": "gpt-5",
                "name": "SilentValley",
            },
        )

        old = _naive_utc(datetime.now(timezone.utc) - timedelta(days=2))
        expires = _naive_utc(datetime.now(timezone.utc) + timedelta(hours=1))
        async with get_session() as session:
            project = await session.get(Project, project_result.data["id"])
            stale_agent = (
                await session.execute(
                    sa_select(Agent).where(cast(Any, Agent.name) == stale.data["name"])
                )
            ).scalar_one()
            assert project is not None
            assert project.id is not None
            assert stale_agent is not None
            assert stale_agent.id is not None
            await session.execute(
                sa_update(Agent)
                .where(cast(Any, Agent.id) == stale_agent.id)
                .values(last_active_ts=old, retired_at=None)
            )
            session.add(
                FileReservation(
                    project_id=project.id,
                    agent_id=stale_agent.id,
                    path_pattern="src/critical.py",
                    exclusive=True,
                    reason="still owned",
                    expires_ts=expires,
                )
            )
            await session.commit()

        protected = await client.call_tool(
            "sweep_stale_agents",
            {
                "project_key": "/sweep/reservations",
                "agent_name": caller.data["name"],
                "registration_token": caller.data["registration_token"],
                "threshold_seconds": 60,
            },
        )
        assert protected.data["retired"] == []

        forced = await client.call_tool(
            "sweep_stale_agents",
            {
                "project_key": "/sweep/reservations",
                "agent_name": caller.data["name"],
                "registration_token": caller.data["registration_token"],
                "threshold_seconds": 60,
                "require_no_active_reservations": False,
            },
        )
        assert forced.data["retired"] == [stale.data["name"]]

        repeated = await client.call_tool(
            "sweep_stale_agents",
            {
                "project_key": "/sweep/reservations",
                "agent_name": caller.data["name"],
                "registration_token": caller.data["registration_token"],
                "threshold_seconds": 60,
                "require_no_active_reservations": False,
            },
        )
        assert repeated.data["retired"] == []
