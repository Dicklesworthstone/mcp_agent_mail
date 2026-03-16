"""Tests for human identity registration and management."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app


async def _seed_project(slug: str = "test-project") -> str:
    """Create a project in the DB and return its slug."""
    await ensure_schema()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT OR IGNORE INTO projects (slug, human_key, created_at) "
                "VALUES (:s, :h, datetime('now'))"
            ),
            {"s": slug, "h": f"/tmp/{slug}"},
        )
        await session.commit()
    return slug


@pytest.mark.asyncio
async def test_register_human_identity(isolated_env):
    """POST /mail/human/register creates a human agent in the project."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
            "display_label": "Lee Gonzales",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "lee"
        assert data["model"] == "Human"
        assert data["program"] == "WebUI"


@pytest.mark.asyncio
async def test_register_human_identity_duplicate(isolated_env):
    """Registering same name twice in same project returns existing."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_register_human_identity_bad_project(isolated_env):
    """Registering in non-existent project returns 404."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/human/register", json={
            "project_slug": "nonexistent",
            "name": "lee",
        })
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_human_identities(isolated_env):
    """GET /mail/human/identities lists all human agents across projects."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.get("/mail/human/identities")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["identities"]) >= 1
        assert any(i["name"] == "lee" for i in data["identities"])


async def _create_message_to_human(slug: str, sender_name: str, subject: str, body: str) -> int:
    """Create an AI agent and send a message to the human 'lee' in the given project."""
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": slug}
        )).fetchone()
        pid = pid_row[0]

        # Create AI agent sender
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test agent',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid, "name": sender_name, "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

        # Get agent IDs
        lee_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'lee'"),
            {"pid": pid},
        )).fetchone()
        sender_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = :n"),
            {"pid": pid, "n": sender_name},
        )).fetchone()

        # Send message
        result = await session.execute(
            text("""INSERT INTO messages
                    (project_id, sender_id, subject, body_md, importance, created_ts, ack_required)
                    VALUES (:pid, :sid, :subj, :body, 'normal', :ts, 0)
                    RETURNING id"""),
            {"pid": pid, "sid": sender_row[0], "subj": subject,
             "body": body, "ts": datetime.now(timezone.utc)},
        )
        mid = result.fetchone()[0]
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            {"mid": mid, "aid": lee_row[0]},
        )
        await session.commit()
    return mid


@pytest.mark.asyncio
async def test_human_inbox_html(isolated_env):
    """GET /mail/human/inbox renders HTML inbox for human identities."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.get("/mail/human/inbox")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_human_inbox_json(isolated_env):
    """GET /mail/human/inbox/api returns JSON inbox data."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })

    mid = await _create_message_to_human(slug, "BrassAdama", "Fleet status report", "All systems nominal.")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/human/inbox/api")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["subject"] == "Fleet status report"
        assert data["messages"][0]["sender"] == "BrassAdama"
        assert data["messages"][0]["read"] is False


@pytest.mark.asyncio
async def test_human_inbox_mark_read(isolated_env):
    """POST /mail/human/inbox/mark-read marks messages as read."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })

    mid = await _create_message_to_human(slug, "TestBot", "Test", "Body")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mark as read
        resp = await client.post("/mail/human/inbox/mark-read", json={
            "message_ids": [mid],
        })
        assert resp.status_code == 200

        # Verify it's now read
        resp = await client.get("/mail/human/inbox/api")
        data = resp.json()
        assert data["messages"][0]["read"] is True


async def _create_ai_agent(slug: str, name: str) -> None:
    """Create an AI agent in the given project."""
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": slug}
        )).fetchone()
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid_row[0], "name": name, "ts": datetime.now(timezone.utc)},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_human_compose_page(isolated_env):
    """GET /mail/human/compose renders compose page with identity selector."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.get(f"/mail/human/compose?project={slug}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_human_send_as_identity(isolated_env):
    """POST /mail/human/send sends message from chosen human identity."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })

    await _create_ai_agent(slug, "SteelGuard")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/human/send", json={
            "project_slug": slug,
            "sender_name": "lee",
            "recipients": ["SteelGuard"],
            "subject": "Review the security audit",
            "body_md": "Please check the latest findings.",
            "importance": "normal",
            "include_preamble": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["sender"] == "lee"

    # Verify message in DB has no preamble
    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT body_md, importance FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert "HUMAN OVERSEER" not in msg[0]
        assert msg[1] == "normal"


@pytest.mark.asyncio
async def test_human_send_with_preamble(isolated_env):
    """POST /mail/human/send with include_preamble=True adds operator preamble."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })

    await _create_ai_agent(slug, "DeepWatch")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/human/send", json={
            "project_slug": slug,
            "sender_name": "lee",
            "recipients": ["DeepWatch"],
            "subject": "Urgent directive",
            "body_md": "Drop everything.",
            "importance": "urgent",
            "include_preamble": True,
        })
        assert resp.status_code == 200
        data = resp.json()

    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT body_md, importance FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert "MESSAGE FROM HUMAN" in msg[0]
        assert msg[1] == "urgent"


@pytest.mark.asyncio
async def test_human_send_validates_sender_is_human(isolated_env):
    """Cannot send as an AI agent identity."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    await _create_ai_agent(slug, "FakeBot")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/human/send", json={
            "project_slug": slug,
            "sender_name": "FakeBot",
            "recipients": ["FakeBot"],
            "subject": "Spoofed",
            "body_md": "This should fail.",
        })
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_human_dashboard(isolated_env):
    """GET /mail/human renders a dashboard landing page."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.get("/mail/human")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_base_template_has_human_link(isolated_env):
    """The unified inbox page should include a link to /mail/human."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail")
        assert resp.status_code == 200
        assert "/mail/human" in resp.text


@pytest.mark.asyncio
async def test_create_note(isolated_env):
    """POST /mail/human/notes creates a private note."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.post("/mail/human/notes", json={
            "project_slug": slug,
            "author": "lee",
            "body_md": "BrassAdama seems to be handling fleet ops well. Monitor for 48h.",
            "thread_id": "thread-42",
            "tags": ["observation", "fleet"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert data["author"] == "lee"


@pytest.mark.asyncio
async def test_list_notes(isolated_env):
    """GET /mail/human/notes/api lists all notes, optionally filtered."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        await client.post("/mail/human/notes", json={
            "project_slug": slug,
            "author": "lee",
            "body_md": "Note 1",
            "tags": ["fleet"],
        })
        await client.post("/mail/human/notes", json={
            "project_slug": slug,
            "author": "lee",
            "body_md": "Note 2",
            "tags": ["security"],
        })

        # All notes
        resp = await client.get("/mail/human/notes/api")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["notes"]) == 2

        # Filter by tag
        resp = await client.get("/mail/human/notes/api?tag=fleet")
        data = resp.json()
        assert len(data["notes"]) == 1
        assert "Note 1" in data["notes"][0]["body_md"]


@pytest.mark.asyncio
async def test_notes_not_visible_to_agents(isolated_env):
    """Notes should NOT appear in agent inboxes or unified inbox."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        await client.post("/mail/human/notes", json={
            "project_slug": slug,
            "author": "lee",
            "body_md": "Secret observation",
        })

        # Check unified inbox -- note should not appear
        resp = await client.get("/mail/api/unified-inbox")
        data = resp.json()
        for msg in data.get("messages", []):
            assert "Secret observation" not in msg.get("subject", "")
            assert "Secret observation" not in msg.get("excerpt", "")


@pytest.mark.asyncio
async def test_delete_note(isolated_env):
    """DELETE /mail/human/notes/{id} removes a note."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/mail/human/register", json={
            "project_slug": slug,
            "name": "lee",
        })
        resp = await client.post("/mail/human/notes", json={
            "project_slug": slug,
            "author": "lee",
            "body_md": "Temporary note",
        })
        note_id = resp.json()["id"]

        resp = await client.delete(f"/mail/human/notes/{note_id}")
        assert resp.status_code == 200

        resp = await client.get("/mail/human/notes/api")
        assert len(resp.json()["notes"]) == 0


async def _create_agent_message_to_lee(client, slug: str) -> int:
    """Helper: register lee, create ReplyBot, send message to lee with thread_id, return message_id."""
    await client.post("/mail/human/register", json={
        "project_slug": slug, "name": "lee",
    })
    async with get_session() as session:
        pid = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": slug}
        )).fetchone()[0]
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, 'ReplyBot', 'claude-code', 'opus-4', 'test',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid, "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

        bot_id = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'ReplyBot'"),
            {"pid": pid},
        )).fetchone()[0]
        lee_id = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'lee'"),
            {"pid": pid},
        )).fetchone()[0]

        result = await session.execute(
            text("""INSERT INTO messages
                    (project_id, sender_id, subject, body_md, importance,
                     thread_id, created_ts, ack_required)
                    VALUES (:pid, :sid, 'Need approval', 'PR #42 ready for review.',
                            'high', 'thread-42', :ts, 0) RETURNING id"""),
            {"pid": pid, "sid": bot_id, "ts": datetime.now(timezone.utc)},
        )
        mid = result.fetchone()[0]
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            {"mid": mid, "aid": lee_id},
        )
        await session.commit()
    return mid


@pytest.mark.asyncio
async def test_human_reply_page(isolated_env):
    """GET /mail/human/reply/{mid} renders reply composer pre-filled with thread context."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mid = await _create_agent_message_to_lee(client, slug)
        resp = await client.get(f"/mail/human/reply/{mid}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_human_reply_sends_in_thread(isolated_env):
    """POST /mail/human/reply sends reply in same thread, addressed to original sender."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mid = await _create_agent_message_to_lee(client, slug)
        resp = await client.post("/mail/human/reply", json={
            "original_message_id": mid,
            "sender_name": "lee",
            "body_md": "Approved. Merge it.",
            "importance": "normal",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Re: Need approval" in data.get("subject", "")

    # Verify thread_id inherited
    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT thread_id, subject FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert msg[0] == "thread-42"
        assert msg[1].startswith("Re:")


@pytest.mark.asyncio
async def test_human_reply_defaults_recipient_to_sender(isolated_env):
    """Reply auto-addresses to the original message sender."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    slug = await _seed_project()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mid = await _create_agent_message_to_lee(client, slug)
        resp = await client.post("/mail/human/reply", json={
            "original_message_id": mid,
            "sender_name": "lee",
            "body_md": "Thanks.",
        })
        data = resp.json()
        assert "ReplyBot" in data["recipients"]
