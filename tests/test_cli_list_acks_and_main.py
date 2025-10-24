from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project


def _seed_with_ack() -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            p = Project(slug="backend", human_key="Backend")
            session.add(p)
            await session.commit()
            await session.refresh(p)
            a = Agent(project_id=p.id, name="Blue", program="x", model="y", task_description="")
            session.add(a)
            await session.commit()
            await session.refresh(a)
            m = Message(
                project_id=p.id,
                sender_id=a.id,
                subject="NeedAck",
                body_md="b",
                ack_required=True,
                importance="normal",
            )
            session.add(m)
            await session.commit()
            await session.refresh(m)
            session.add(
                MessageRecipient(message_id=m.id, agent_id=a.id, kind="to")
            )
            await session.commit()
    asyncio.run(_seed())


def test_cli_list_acks_runs(isolated_env):
    _seed_with_ack()
    runner = CliRunner()
    res = runner.invoke(app, ["list-acks", "--project", "Backend", "--agent", "Blue", "--limit", "5"])
    assert res.exit_code == 0


def test_cli_lint_command(monkeypatch):
    # Verify lint command wiring
    called: dict[str, bool] = {"ok": False}
    def fake_run(cmd: list[str]) -> None:
        called["ok"] = True
    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    runner = CliRunner()
    r = runner.invoke(app, ["lint"])  # smoke test
    assert r.exit_code == 0
    assert called["ok"] is True


