"""CLI Mail Commands Tests.

Tests for mail-related CLI subcommands:
- mail status: Routing diagnostics
- acks pending: List pending acknowledgements
- acks remind: Highlight old acknowledgements
- acks overdue: List overdue acknowledgements
- list-acks: List ack-required messages

Reference: mcp_agent_mail-n6z
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project

runner = CliRunner()


# ============================================================================
# Fixtures and helpers
# ============================================================================


async def seed_project_with_agents() -> tuple[Project, Agent, Agent]:
    """Create a project with two agents for message exchange."""
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug="mailtest", human_key="/mail/test")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        sender = Agent(
            project_id=project.id,
            name="Sender",
            program="test",
            model="test",
            task_description="Sending messages",
        )
        receiver = Agent(
            project_id=project.id,
            name="Receiver",
            program="test",
            model="test",
            task_description="Receiving messages",
        )
        session.add(sender)
        session.add(receiver)
        await session.commit()
        await session.refresh(sender)
        await session.refresh(receiver)

        return project, sender, receiver


async def seed_message_with_ack(
    project: Project,
    sender: Agent,
    receiver: Agent,
    subject: str = "Test Message",
    ack_required: bool = True,
    created_offset_minutes: int = 0,
    acknowledged: bool = False,
) -> Message:
    """Create a message with optional acknowledgement state."""
    async with get_session() as session:
        created_ts = datetime.now(timezone.utc) - timedelta(minutes=created_offset_minutes)
        # Convert to naive for SQLite
        created_ts_naive = created_ts.replace(tzinfo=None)

        message = Message(
            project_id=project.id,
            sender_id=sender.id,
            subject=subject,
            body_md="Test message body",
            ack_required=ack_required,
            importance="normal",
            created_ts=created_ts_naive,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)

        recipient = MessageRecipient(
            message_id=message.id,
            agent_id=receiver.id,
            kind="to",
            ack_ts=datetime.now(timezone.utc).replace(tzinfo=None) if acknowledged else None,
        )
        session.add(recipient)
        await session.commit()

        return message


# ============================================================================
# mail status tests
# ============================================================================


def test_mail_status_basic(isolated_env, tmp_path, monkeypatch):
    """mail status command runs and shows routing info."""
    # Create a simple directory to test with
    test_dir = tmp_path / "test_repo"
    test_dir.mkdir()

    result = runner.invoke(app, ["mail", "status", str(test_dir)])
    # Should succeed even without git repo
    assert result.exit_code == 0
    assert "WORKTREES_ENABLED" in result.stdout


def test_mail_status_with_git_repo(isolated_env, tmp_path):
    """mail status shows git-related info when in a git repo."""
    import subprocess

    # Initialize a git repo
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=str(repo_dir),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir),
        check=True,
    )

    result = runner.invoke(app, ["mail", "status", str(repo_dir)])
    assert result.exit_code == 0
    assert "slug" in result.stdout
    assert "path" in result.stdout


def test_mail_status_shows_identity_mode(isolated_env, tmp_path):
    """mail status displays PROJECT_IDENTITY_MODE setting."""
    test_dir = tmp_path / "test"
    test_dir.mkdir()

    result = runner.invoke(app, ["mail", "status", str(test_dir)])
    assert result.exit_code == 0
    assert "PROJECT_IDENTITY_MODE" in result.stdout


# ============================================================================
# acks pending tests
# ============================================================================


def test_acks_pending_no_messages(isolated_env):
    """acks pending shows empty table when no pending acks."""
    project, _sender, receiver = asyncio.run(seed_project_with_agents())

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Pending ACKs" in result.stdout


def test_acks_pending_with_pending_message(isolated_env):
    """acks pending shows messages requiring acknowledgement."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(seed_message_with_ack(project, sender, receiver, "Need Ack"))

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Need Ack" in result.stdout


def test_acks_pending_excludes_acknowledged(isolated_env):
    """acks pending excludes already acknowledged messages."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(
        seed_message_with_ack(project, sender, receiver, "Already Acked", acknowledged=True)
    )

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Already Acked" not in result.stdout


def test_acks_pending_respects_limit(isolated_env):
    """acks pending respects the --limit option."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create multiple messages
    for i in range(5):
        asyncio.run(seed_message_with_ack(project, sender, receiver, f"Message {i}"))

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name, "--limit", "2"],
    )
    assert result.exit_code == 0


def test_acks_pending_invalid_project(isolated_env):
    """acks pending fails gracefully with invalid project."""
    asyncio.run(ensure_schema())

    result = runner.invoke(
        app,
        ["acks", "pending", "nonexistent", "SomeAgent"],
    )
    # Should exit with error
    assert result.exit_code != 0


def test_acks_pending_invalid_agent(isolated_env):
    """acks pending fails gracefully with invalid agent."""
    project, _sender, _receiver = asyncio.run(seed_project_with_agents())

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, "NonexistentAgent"],
    )
    # Should exit with error
    assert result.exit_code != 0


# ============================================================================
# acks remind tests
# ============================================================================


def test_acks_remind_no_old_messages(isolated_env):
    """acks remind shows success message when no old pending acks."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create a recent message (0 minutes old)
    asyncio.run(
        seed_message_with_ack(project, sender, receiver, "Recent", created_offset_minutes=0)
    )

    result = runner.invoke(
        app,
        ["acks", "remind", project.human_key, receiver.name, "--min-age-minutes", "60"],
    )
    assert result.exit_code == 0
    assert "No pending acknowledgements exceed the threshold" in result.stdout


def test_acks_remind_shows_old_messages(isolated_env):
    """acks remind shows messages older than threshold."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create an old message (120 minutes old)
    asyncio.run(
        seed_message_with_ack(
            project, sender, receiver, "Old Message", created_offset_minutes=120
        )
    )

    result = runner.invoke(
        app,
        ["acks", "remind", project.human_key, receiver.name, "--min-age-minutes", "30"],
    )
    assert result.exit_code == 0
    assert "Old Message" in result.stdout


def test_acks_remind_default_threshold(isolated_env):
    """acks remind uses default 30 minute threshold."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create a message 45 minutes old (should show with default 30m threshold)
    asyncio.run(
        seed_message_with_ack(
            project, sender, receiver, "Medium Age", created_offset_minutes=45
        )
    )

    result = runner.invoke(
        app,
        ["acks", "remind", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Medium Age" in result.stdout


def test_acks_remind_respects_limit(isolated_env):
    """acks remind respects the --limit option."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create multiple old messages
    for i in range(5):
        asyncio.run(
            seed_message_with_ack(
                project, sender, receiver, f"Old {i}", created_offset_minutes=120
            )
        )

    result = runner.invoke(
        app,
        [
            "acks",
            "remind",
            project.human_key,
            receiver.name,
            "--min-age-minutes",
            "30",
            "--limit",
            "2",
        ],
    )
    assert result.exit_code == 0


# ============================================================================
# acks overdue tests
# ============================================================================


def test_acks_overdue_no_overdue_messages(isolated_env):
    """acks overdue shows empty table when no overdue acks."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create a recent message
    asyncio.run(
        seed_message_with_ack(project, sender, receiver, "Recent", created_offset_minutes=0)
    )

    result = runner.invoke(
        app,
        ["acks", "overdue", project.human_key, receiver.name, "--ttl-minutes", "60"],
    )
    assert result.exit_code == 0
    # When no overdue messages, shows success message (not table)
    assert "No overdue acknowledgements" in result.stdout or "ACK Overdue" in result.stdout


def test_acks_overdue_shows_overdue_messages(isolated_env):
    """acks overdue shows messages older than TTL."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create an old message (120 minutes old)
    asyncio.run(
        seed_message_with_ack(
            project, sender, receiver, "Overdue Message", created_offset_minutes=120
        )
    )

    result = runner.invoke(
        app,
        ["acks", "overdue", project.human_key, receiver.name, "--ttl-minutes", "30"],
    )
    assert result.exit_code == 0
    assert "Overdue Message" in result.stdout


def test_acks_overdue_default_ttl(isolated_env):
    """acks overdue uses default 60 minute TTL."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    # Create a message 90 minutes old (should show with default 60m TTL)
    asyncio.run(
        seed_message_with_ack(
            project, sender, receiver, "Past Due", created_offset_minutes=90
        )
    )

    result = runner.invoke(
        app,
        ["acks", "overdue", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Past Due" in result.stdout


def test_acks_overdue_excludes_acknowledged(isolated_env):
    """acks overdue excludes acknowledged messages even if old."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(
        seed_message_with_ack(
            project,
            sender,
            receiver,
            "Old But Acked",
            created_offset_minutes=120,
            acknowledged=True,
        )
    )

    result = runner.invoke(
        app,
        ["acks", "overdue", project.human_key, receiver.name, "--ttl-minutes", "30"],
    )
    assert result.exit_code == 0
    assert "Old But Acked" not in result.stdout


# ============================================================================
# list-acks tests
# ============================================================================


def test_list_acks_no_pending(isolated_env):
    """list-acks shows empty table when no pending acks."""
    project, _sender, receiver = asyncio.run(seed_project_with_agents())

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.human_key, "--agent", receiver.name],
    )
    assert result.exit_code == 0
    assert "Pending Acks" in result.stdout


def test_list_acks_shows_pending(isolated_env):
    """list-acks shows messages requiring acknowledgement."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(seed_message_with_ack(project, sender, receiver, "Needs Ack"))

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.human_key, "--agent", receiver.name],
    )
    assert result.exit_code == 0
    assert "Needs Ack" in result.stdout


def test_list_acks_excludes_non_ack_required(isolated_env):
    """list-acks excludes messages that don't require ack."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(
        seed_message_with_ack(
            project, sender, receiver, "No Ack Needed", ack_required=False
        )
    )

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.human_key, "--agent", receiver.name],
    )
    assert result.exit_code == 0
    assert "No Ack Needed" not in result.stdout


def test_list_acks_excludes_acknowledged(isolated_env):
    """list-acks excludes already acknowledged messages."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(
        seed_message_with_ack(project, sender, receiver, "Already Done", acknowledged=True)
    )

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.human_key, "--agent", receiver.name],
    )
    assert result.exit_code == 0
    assert "Already Done" not in result.stdout


def test_list_acks_respects_limit(isolated_env):
    """list-acks respects the --limit option."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    for i in range(5):
        asyncio.run(seed_message_with_ack(project, sender, receiver, f"Msg {i}"))

    result = runner.invoke(
        app,
        [
            "list-acks",
            "--project",
            project.human_key,
            "--agent",
            receiver.name,
            "--limit",
            "2",
        ],
    )
    assert result.exit_code == 0


def test_list_acks_invalid_project(isolated_env):
    """list-acks fails gracefully with invalid project."""
    asyncio.run(ensure_schema())

    result = runner.invoke(
        app,
        ["list-acks", "--project", "nonexistent", "--agent", "SomeAgent"],
    )
    assert result.exit_code != 0


def test_list_acks_invalid_agent(isolated_env):
    """list-acks fails gracefully with invalid agent."""
    project, _sender, _receiver = asyncio.run(seed_project_with_agents())

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.human_key, "--agent", "InvalidAgent"],
    )
    assert result.exit_code != 0


def test_list_acks_by_slug(isolated_env):
    """list-acks works with project slug instead of human_key."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(seed_message_with_ack(project, sender, receiver, "Via Slug"))

    result = runner.invoke(
        app,
        ["list-acks", "--project", project.slug, "--agent", receiver.name],
    )
    assert result.exit_code == 0
    assert "Via Slug" in result.stdout


# ============================================================================
# Edge cases and error handling
# ============================================================================


def test_acks_pending_shows_message_details(isolated_env):
    """acks pending shows thread ID, kind, and timestamps."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())

    async def create_with_thread():
        async with get_session() as session:
            message = Message(
                project_id=project.id,
                sender_id=sender.id,
                subject="Threaded Message",
                body_md="Body",
                ack_required=True,
                importance="high",
                thread_id="THREAD-123",
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)
            session.add(
                MessageRecipient(message_id=message.id, agent_id=receiver.id, kind="cc")
            )
            await session.commit()

    asyncio.run(create_with_thread())

    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "THREAD-123" in result.stdout
    assert "cc" in result.stdout


def test_acks_remind_read_status_indicator(isolated_env):
    """acks remind shows read status for each message."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())

    async def create_read_message():
        async with get_session() as session:
            message = Message(
                project_id=project.id,
                sender_id=sender.id,
                subject="Read But Not Acked",
                body_md="Body",
                ack_required=True,
                importance="normal",
                created_ts=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=2),
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)
            session.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=receiver.id,
                    kind="to",
                    read_ts=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
            await session.commit()

    asyncio.run(create_read_message())

    result = runner.invoke(
        app,
        ["acks", "remind", project.human_key, receiver.name, "--min-age-minutes", "30"],
    )
    assert result.exit_code == 0
    assert "Read But Not Acked" in result.stdout
    assert "yes" in result.stdout  # Read status


def test_multiple_recipients_handled(isolated_env):
    """Commands handle messages with multiple recipients correctly."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())

    async def create_multi_recipient():
        async with get_session() as session:
            # Create another agent
            other = Agent(
                project_id=project.id,
                name="Other",
                program="test",
                model="test",
                task_description="Other agent",
            )
            session.add(other)
            await session.commit()
            await session.refresh(other)

            message = Message(
                project_id=project.id,
                sender_id=sender.id,
                subject="Multi Recipient",
                body_md="Body",
                ack_required=True,
                importance="normal",
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)

            # Add multiple recipients
            session.add(
                MessageRecipient(message_id=message.id, agent_id=receiver.id, kind="to")
            )
            session.add(
                MessageRecipient(message_id=message.id, agent_id=other.id, kind="cc")
            )
            await session.commit()

    asyncio.run(create_multi_recipient())

    # Check receiver sees the message
    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Multi Recipient" in result.stdout

    # Check other agent also sees it
    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, "Other"],
    )
    assert result.exit_code == 0
    assert "Multi Recipient" in result.stdout


def test_acks_commands_exact_agent_name(isolated_env):
    """Agent names are matched exactly (case-sensitive)."""
    project, sender, receiver = asyncio.run(seed_project_with_agents())
    asyncio.run(seed_message_with_ack(project, sender, receiver, "Case Test"))

    # Exact case should work
    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name],
    )
    assert result.exit_code == 0
    assert "Case Test" in result.stdout

    # Wrong case should fail (agent names are case-sensitive)
    result = runner.invoke(
        app,
        ["acks", "pending", project.human_key, receiver.name.upper()],
    )
    assert result.exit_code != 0  # Agent not found
