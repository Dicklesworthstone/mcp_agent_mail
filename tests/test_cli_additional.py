from __future__ import annotations

import asyncio
from typing import Any

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project


def _seed_with_ack() -> dict[str, Any]:
    data: dict[str, Any] = {}

    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            p = Project(slug="backend", human_key="Backend")
            session.add(p)
            await session.commit()
            await session.refresh(p)
            a = Agent(project_id=p.id, name="A", program="x", model="y")
            b = Agent(project_id=p.id, name="B", program="x", model="y")
            session.add(a)
            session.add(b)
            await session.commit()
            await session.refresh(a)
            await session.refresh(b)
            # Ack-required message from A to B
            m = Message(
                project_id=p.id, sender_id=a.id, subject="NeedsAck", body_md="body", ack_required=True
            )
            session.add(m)
            await session.flush()
            session.add(MessageRecipient(message_id=m.id, agent_id=b.id, kind="to"))
            await session.commit()
            data["project_id"] = p.id
            data["agent_b_name"] = b.name
            # No file reservations needed for this test; we only exercise CLI output

    asyncio.run(_seed())
    return data


def test_cli_file_reservations_soon_and_list_acks_and_remind(isolated_env):
    payload = _seed_with_ack()
    runner = CliRunner()

    # file_reservations soon should show table (smoke)
    res_file_reservations = runner.invoke(app, ["file_reservations", "soon", "Backend", "--minutes", "5"])
    assert res_file_reservations.exit_code == 0

    # list-acks should render a table without error
    res_acks = runner.invoke(app, [
        "list-acks",
        "--project",
        "Backend",
        "--agent",
        payload["agent_b_name"],
        "--limit",
        "10",
    ])
    assert res_acks.exit_code == 0
    assert "Pending Acks" in res_acks.stdout

    # remind with age threshold 0 should run and not error
    res_remind = runner.invoke(
        app,
        ["acks", "remind", "Backend", payload["agent_b_name"], "--min-age-minutes", "0", "--limit", "5"],
    )
    assert res_remind.exit_code == 0


