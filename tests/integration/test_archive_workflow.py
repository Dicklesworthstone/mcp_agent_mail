import asyncio
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project
from mcp_agent_mail.share import resolve_sqlite_database_path


@pytest.mark.filterwarnings("ignore:.*ResourceWarning")
def test_archive_save_list_restore_cycle(isolated_env):
    runner = CliRunner()
    storage_root = Path(os.environ["STORAGE_ROOT"])
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "README.txt").write_text("mail-data", encoding="utf-8")

    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="demo", human_key="Demo")
            session.add(project)
            await session.commit()
            await session.refresh(project)

            agent = Agent(project_id=project.id, name="BlueLake", program="codex", model="gpt5")
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

            message = Message(
                project_id=project.id,
                sender_id=agent.id,
                subject="Disaster drill",
                body_md="Ack me",
                ack_required=True,
                attachments=[{"type": "file", "path": "attachments/demo.txt"}],
            )
            session.add(message)
            await session.commit()
            await session.refresh(message)

            recipient = MessageRecipient(
                message_id=message.id,
                agent_id=agent.id,
                kind="to",
                read_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
                ack_ts=datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
            session.add(recipient)
            await session.commit()

    asyncio.run(_seed())

    with runner.isolated_filesystem():
        archive_dir = Path.cwd() / cli_module.ARCHIVE_DIR_NAME
        before = set(archive_dir.glob("*.zip")) if archive_dir.exists() else set()

        save_result = runner.invoke(app, ["archive", "save", "--label", "integration-test"])
        assert save_result.exit_code == 0

        archive_dir.mkdir(parents=True, exist_ok=True)
        created = set(archive_dir.glob("*.zip")) - before
        assert len(created) == 1
        archive_path = created.pop()

        list_result = runner.invoke(app, ["archive", "list", "--json"])
        assert list_result.exit_code == 0
        assert archive_path.name in list_result.stdout

        database_path = resolve_sqlite_database_path()
        if database_path.exists():
            database_path.unlink()
        wal_path = Path(f"{database_path}-wal")
        if wal_path.exists():
            wal_path.unlink()
        shm_path = Path(f"{database_path}-shm")
        if shm_path.exists():
            shm_path.unlink()
        if storage_root.exists():
            shutil.rmtree(storage_root)

        restore_result = runner.invoke(app, ["archive", "restore", str(archive_path), "--force"])
        assert restore_result.exit_code == 0

    restored_db = resolve_sqlite_database_path()
    conn = sqlite3.connect(restored_db)
    try:
        conn.row_factory = sqlite3.Row
        msg_row = conn.execute("SELECT ack_required, subject FROM messages LIMIT 1").fetchone()
        assert msg_row["ack_required"] == 1
        recipient_row = conn.execute("SELECT read_ts, ack_ts FROM message_recipients LIMIT 1").fetchone()
        assert recipient_row["read_ts"] is not None
        assert recipient_row["ack_ts"] is not None
    finally:
        conn.close()

    restored_storage_root = Path(os.environ["STORAGE_ROOT"])
    restored_marker = restored_storage_root / "README.txt"
    assert restored_marker.exists()
    assert restored_marker.read_text(encoding="utf-8") == "mail-data"
