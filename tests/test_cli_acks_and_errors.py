from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project


def _seed_ack_data() -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as s:
            p = Project(slug="backend", human_key="Backend")
            s.add(p)
            await s.commit()
            await s.refresh(p)
            a = Agent(project_id=p.id, name="Blue", program="x", model="y", task_description="")
            s.add(a)
            await s.commit()
            await s.refresh(a)
            # create backdated ack-required messages
            old = datetime.now(timezone.utc) - timedelta(minutes=90)
            m1 = Message(project_id=p.id, sender_id=a.id, subject="Pending", body_md="x", ack_required=True, created_ts=old)
            s.add(m1)
            await s.commit()
            await s.refresh(m1)
            s.add(MessageRecipient(message_id=m1.id, agent_id=a.id, kind="to", read_ts=None, ack_ts=None))
            await s.commit()
    asyncio.run(_seed())


def test_cli_acks_views_with_data(isolated_env):
    _seed_ack_data()
    runner = CliRunner()
    res = runner.invoke(app, ["acks", "pending", "Backend", "Blue", "--limit", "10"])
    assert res.exit_code == 0
    res2 = runner.invoke(app, ["acks", "remind", "Backend", "Blue", "--min-age-minutes", "30", "--limit", "10"])
    assert res2.exit_code == 0
    res3 = runner.invoke(app, ["acks", "overdue", "Backend", "Blue", "--ttl-minutes", "60", "--limit", "10"])
    assert res3.exit_code == 0


def test_cli_list_acks_command_smoke(isolated_env):
    _seed_ack_data()
    runner = CliRunner()
    res = runner.invoke(app, ["list-acks", "--project", "Backend", "--agent", "Blue", "--limit", "5"])
    assert res.exit_code == 0


