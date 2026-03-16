# Human-Agent Messaging Symmetry — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow Lee to appear as a regular agent in the agent-mail system — send messages, receive replies, reply in threads, write notes, and manage identity — with full parity to AI agents.

**Architecture:** Replace the hardcoded "HumanOverseer" broadcast-only pattern with a configurable human identity system. Humans register as named agents (e.g., `lee`) with `model="Human"`. The existing Agent/Message/MessageRecipient schema requires zero changes. New HTTP routes and templates provide inbox, compose, reply, and notes UI. The existing `overseer/send` endpoint is preserved for backwards compatibility but the new compose page replaces it as the primary interface.

**Tech Stack:** Python 3.12+, FastAPI, SQLModel, Jinja2, Alpine.js, Tailwind CSS (CDN), SQLite

**Repo:** `/Users/leegonzales/Projects/leegonzales/mcp_agent_mail/`

---

## Task 1: Human Identity Registration — Backend

Register Lee as a named human agent in any project, replacing the implicit HumanOverseer auto-creation with explicit identity management.

**Files:**
- Modify: `src/mcp_agent_mail/http.py` (add 2 new routes)
- No model changes needed — Agent model already supports `model="Human"`, `program="WebUI"`

**Step 1: Write the failing test**

Create `tests/test_human_identity.py`:

```python
"""Tests for human identity registration and management."""

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def app():
    """Create a fresh app instance with clean DB."""
    from mcp_agent_mail.http import build_app
    fastapi_app = build_app()
    yield fastapi_app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded_project(client):
    """Create a project via the MCP ensure_project flow."""
    from mcp_agent_mail.db import get_session, ensure_schema
    from sqlalchemy import text
    await ensure_schema()
    async with get_session() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO projects (slug, human_key) VALUES (:s, :h)"),
            {"s": "test-project", "h": "/tmp/test-project"},
        )
        await session.commit()
    return "test-project"


async def test_register_human_identity(client, seeded_project):
    """POST /mail/human/register creates a human agent in the project."""
    resp = await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
        "display_label": "Lee Gonzales",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "lee"
    assert data["model"] == "Human"
    assert data["program"] == "WebUI"


async def test_register_human_identity_duplicate(client, seeded_project):
    """Registering same name twice in same project returns existing."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })
    resp = await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })
    assert resp.status_code == 200


async def test_register_human_identity_bad_project(client):
    """Registering in non-existent project returns 404."""
    resp = await client.post("/mail/human/register", json={
        "project_slug": "nonexistent",
        "name": "lee",
    })
    assert resp.status_code == 404


async def test_list_human_identities(client, seeded_project):
    """GET /mail/human/identities lists all human agents across projects."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })
    resp = await client.get("/mail/human/identities")
    assert resp.status_code == 200
    # Returns JSON with identities list
    data = resp.json()
    assert len(data["identities"]) >= 1
    assert any(i["name"] == "lee" for i in data["identities"])
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/leegonzales/Projects/leegonzales/mcp_agent_mail && python -m pytest tests/test_human_identity.py -v`
Expected: FAIL — routes don't exist yet (404 on POST/GET)

**Step 3: Implement the routes**

Add to `http.py`, inside the `build_app()` function, near the existing overseer routes (~line 2120):

```python
@fastapi_app.post("/mail/human/register")
async def human_register(request: Request) -> JSONResponse:
    """Register a human identity as an agent in a project."""
    await ensure_schema()

    body = await request.json()
    project_slug: str = body.get("project_slug", "").strip()
    name: str = body.get("name", "").strip()
    display_label: str = body.get("display_label", "").strip()

    if not project_slug:
        raise HTTPException(status_code=400, detail="project_slug is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > 128:
        raise HTTPException(status_code=400, detail="name too long (max 128)")

    async with get_session() as session:
        prow = (
            await session.execute(
                text("SELECT id, slug FROM projects WHERE slug = :k OR human_key = :k"),
                {"k": project_slug},
            )
        ).fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail="Project not found")

        pid = int(prow[0])
        now = datetime.now(timezone.utc)

        # Upsert: create if missing, return existing if present
        await session.execute(
            text("""
                INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                VALUES (:pid, :name, :prog, :model, :task, :policy, :att, :ts, :ts)
            """),
            {
                "pid": pid, "name": name, "prog": "WebUI", "model": "Human",
                "task": display_label or f"Human operator: {name}",
                "policy": "open", "att": "auto", "ts": now,
            },
        )
        await session.commit()

        row = (
            await session.execute(
                text("SELECT id, name, program, model, task_description, inception_ts FROM agents WHERE project_id = :pid AND name = :name"),
                {"pid": pid, "name": name},
            )
        ).fetchone()

    return JSONResponse({
        "id": row[0], "name": row[1], "program": row[2],
        "model": row[3], "display_label": row[4],
        "created_at": str(row[5]),
    })


@fastapi_app.get("/mail/human/identities")
async def human_identities() -> JSONResponse:
    """List all human agents across all projects."""
    await ensure_schema()
    async with get_session() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT a.id, a.name, a.task_description, a.inception_ts,
                           p.slug, p.human_key
                    FROM agents a
                    JOIN projects p ON a.project_id = p.id
                    WHERE a.model = 'Human'
                    ORDER BY a.name, p.slug
                """)
            )
        ).fetchall()

    identities = [
        {
            "id": r[0], "name": r[1], "display_label": r[2],
            "created_at": str(r[3]), "project_slug": r[4],
            "project_path": r[5],
        }
        for r in rows
    ]
    return JSONResponse({"identities": identities})
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/leegonzales/Projects/leegonzales/mcp_agent_mail && python -m pytest tests/test_human_identity.py -v`
Expected: PASS (all 4 tests)

**Step 5: Commit**

```bash
cd /Users/leegonzales/Projects/leegonzales/mcp_agent_mail
git add tests/test_human_identity.py src/mcp_agent_mail/http.py
git commit -m "feat: add human identity registration routes"
```

---

## Task 2: Human Inbox — Backend + Template

Show Lee his messages — all messages sent TO any of his human agent identities, across all projects, with read/unread tracking and mark-as-read.

**Files:**
- Modify: `src/mcp_agent_mail/http.py` (add 3 new routes)
- Create: `src/mcp_agent_mail/templates/human_inbox.html`

**Step 1: Write the failing test**

Add to `tests/test_human_identity.py`:

```python
async def test_human_inbox_html(client, seeded_project):
    """GET /mail/human/inbox renders HTML inbox for human identities."""
    # Register identity
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })
    resp = await client.get("/mail/human/inbox")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_human_inbox_json(client, seeded_project):
    """GET /mail/human/inbox/api returns JSON inbox data."""
    # Register identity
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })

    # Create an AI agent and send a message TO lee
    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
        )).fetchone()
        pid = pid_row[0]

        # Create AI agent
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test agent',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid, "name": "BrassAdama", "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

        # Get agent IDs
        lee_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'lee'"),
            {"pid": pid},
        )).fetchone()
        adama_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'BrassAdama'"),
            {"pid": pid},
        )).fetchone()

        # Send message from BrassAdama to lee
        result = await session.execute(
            text("""INSERT INTO messages
                    (project_id, sender_id, subject, body_md, importance, created_ts, ack_required)
                    VALUES (:pid, :sid, :subj, :body, 'normal', :ts, 0)
                    RETURNING id"""),
            {"pid": pid, "sid": adama_row[0], "subj": "Fleet status report",
             "body": "All systems nominal.", "ts": datetime.now(timezone.utc)},
        )
        mid = result.fetchone()[0]
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            {"mid": mid, "aid": lee_row[0]},
        )
        await session.commit()

    resp = await client.get("/mail/human/inbox/api")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["subject"] == "Fleet status report"
    assert data["messages"][0]["sender"] == "BrassAdama"
    assert data["messages"][0]["read"] is False


async def test_human_inbox_mark_read(client, seeded_project):
    """POST /mail/human/inbox/mark-read marks messages as read."""
    # Setup: register lee, create message to lee (reuse pattern above)
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })

    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
        )).fetchone()
        pid = pid_row[0]

        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test agent',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid, "name": "TestBot", "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

        bot_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'TestBot'"),
            {"pid": pid},
        )).fetchone()
        lee_row = (await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = 'lee'"),
            {"pid": pid},
        )).fetchone()

        result = await session.execute(
            text("""INSERT INTO messages
                    (project_id, sender_id, subject, body_md, importance, created_ts, ack_required)
                    VALUES (:pid, :sid, 'Test', 'Body', 'normal', :ts, 0)
                    RETURNING id"""),
            {"pid": pid, "sid": bot_row[0], "ts": datetime.now(timezone.utc)},
        )
        mid = result.fetchone()[0]
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            {"mid": mid, "aid": lee_row[0]},
        )
        await session.commit()

    # Mark as read
    resp = await client.post("/mail/human/inbox/mark-read", json={
        "message_ids": [mid],
    })
    assert resp.status_code == 200

    # Verify it's now read
    resp = await client.get("/mail/human/inbox/api")
    data = resp.json()
    assert data["messages"][0]["read"] is True
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_human_identity.py::test_human_inbox_html tests/test_human_identity.py::test_human_inbox_json tests/test_human_identity.py::test_human_inbox_mark_read -v`
Expected: FAIL — routes don't exist

**Step 3: Implement inbox routes**

Add to `http.py` after the registration routes:

```python
@fastapi_app.get("/mail/human/inbox")
async def human_inbox() -> HTMLResponse:
    """Render HTML inbox for all human identities."""
    await ensure_schema()
    payload = await _build_human_inbox_payload()
    return await _render("human_inbox.html", **payload)


@fastapi_app.get("/mail/human/inbox/api")
async def human_inbox_api() -> JSONResponse:
    """JSON API for human inbox (for AJAX refresh)."""
    await ensure_schema()
    payload = await _build_human_inbox_payload()
    return JSONResponse(payload)


@fastapi_app.post("/mail/human/inbox/mark-read")
async def human_inbox_mark_read(request: Request) -> JSONResponse:
    """Mark messages as read for human recipients."""
    await ensure_schema()
    body = await request.json()
    message_ids: list[int] = body.get("message_ids", [])
    if not message_ids or len(message_ids) > 500:
        raise HTTPException(status_code=400, detail="Provide 1-500 message_ids")

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        # Get all human agent IDs
        human_ids = [
            r[0] for r in (
                await session.execute(
                    text("SELECT id FROM agents WHERE model = 'Human'")
                )
            ).fetchall()
        ]
        if not human_ids:
            raise HTTPException(status_code=404, detail="No human identities registered")

        placeholders_mid = ", ".join(f":mid_{i}" for i in range(len(message_ids)))
        placeholders_aid = ", ".join(f":aid_{i}" for i in range(len(human_ids)))
        params = {"ts": now}
        params.update({f"mid_{i}": mid for i, mid in enumerate(message_ids)})
        params.update({f"aid_{i}": aid for i, aid in enumerate(human_ids)})

        await session.execute(
            text(f"""
                UPDATE message_recipients SET read_ts = :ts
                WHERE message_id IN ({placeholders_mid})
                AND agent_id IN ({placeholders_aid})
                AND read_ts IS NULL
            """),
            params,
        )
        await session.commit()

    return JSONResponse({"success": True, "marked": len(message_ids)})
```

Add the helper function near `_build_unified_inbox_payload`:

```python
async def _build_human_inbox_payload(*, limit: int = 200) -> dict[str, Any]:
    """Fetch messages addressed to any human agent."""
    messages: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []

    async with get_session() as session:
        # Get human identities
        id_rows = (
            await session.execute(
                text("""
                    SELECT a.id, a.name, a.task_description, p.slug, p.human_key
                    FROM agents a JOIN projects p ON a.project_id = p.id
                    WHERE a.model = 'Human'
                    ORDER BY a.name
                """)
            )
        ).fetchall()

        identities = [
            {"id": r[0], "name": r[1], "display_label": r[2],
             "project_slug": r[3], "project_path": r[4]}
            for r in id_rows
        ]

        if not id_rows:
            return {"messages": [], "identities": [], "unread_count": 0}

        human_agent_ids = [r[0] for r in id_rows]
        placeholders = ", ".join(f":aid_{i}" for i in range(len(human_agent_ids)))
        params: dict[str, Any] = {"lim": limit}
        params.update({f"aid_{i}": aid for i, aid in enumerate(human_agent_ids)})

        rows = (
            await session.execute(
                text(f"""
                    SELECT m.id, m.subject, m.body_md, m.created_ts, m.importance,
                           m.thread_id, sender.name AS sender_name,
                           p.slug AS project_slug, p.human_key AS project_path,
                           mr.read_ts,
                           recip.name AS recipient_name
                    FROM messages m
                    JOIN message_recipients mr ON m.id = mr.message_id
                    JOIN agents recip ON mr.agent_id = recip.id
                    JOIN agents sender ON m.sender_id = sender.id
                    JOIN projects p ON m.project_id = p.id
                    WHERE mr.agent_id IN ({placeholders})
                    ORDER BY m.created_ts DESC
                    LIMIT :lim
                """),
                params,
            )
        ).fetchall()

    now = datetime.now(timezone.utc)
    unread = 0
    for r in rows:
        created = r[3]
        is_read = r[9] is not None
        if not is_read:
            unread += 1

        # Compute relative time
        if isinstance(created, str):
            from datetime import datetime as dt
            created = dt.fromisoformat(created)
        delta = (now - created).total_seconds()
        if delta < 60:
            relative = "just now"
        elif delta < 3600:
            relative = f"{int(delta // 60)}m ago"
        elif delta < 86400:
            relative = f"{int(delta // 3600)}h ago"
        else:
            relative = f"{int(delta // 86400)}d ago"

        body_text = r[2] or ""
        messages.append({
            "id": r[0],
            "subject": r[1],
            "excerpt": body_text[:200].replace("\n", " "),
            "created_ts": str(r[3]),
            "created_relative": relative,
            "importance": r[4],
            "thread_id": r[5],
            "sender": r[6],
            "project_slug": r[7],
            "project_path": r[8],
            "read": is_read,
            "recipient": r[10],
        })

    return {"messages": messages, "identities": identities, "unread_count": unread}
```

**Step 4: Create inbox template**

Create `src/mcp_agent_mail/templates/human_inbox.html`:

This template extends `base.html` and renders a Gmail-style inbox showing:
- Unread count badge in header
- Filter tabs: All / Unread / By Project
- Message list with sender, subject, excerpt, project badge, relative time
- Unread messages highlighted with left border + bold subject
- Checkbox select for batch mark-as-read
- Click-through to `/mail/{project}/message/{mid}` for full view
- Auto-refresh via polling `/mail/human/inbox/api` every 30s
- Empty state when no messages

Use the same Alpine.js + Tailwind patterns as `mail_unified_inbox.html` and `overseer_compose.html`. Match existing dark mode support, animations, and icon usage.

The template should be ~300 lines, following the exact structure of `mail_unified_inbox.html` but filtered to human-recipient messages only.

**Step 5: Run tests**

Run: `python -m pytest tests/test_human_identity.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tests/test_human_identity.py src/mcp_agent_mail/http.py src/mcp_agent_mail/templates/human_inbox.html
git commit -m "feat: add human inbox with read/unread tracking"
```

---

## Task 3: Human Compose — Sender Identity Selection

Replace the hardcoded "HumanOverseer" compose page with a new compose page that lets Lee choose which identity to send as, control importance level, and optionally skip the aggressive preamble.

**Files:**
- Create: `src/mcp_agent_mail/templates/human_compose.html`
- Modify: `src/mcp_agent_mail/http.py` (add new compose route + send route)

**Step 1: Write the failing test**

Add to `tests/test_human_identity.py`:

```python
async def test_human_compose_page(client, seeded_project):
    """GET /mail/human/compose renders compose page with identity selector."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })
    resp = await client.get(f"/mail/human/compose?project={seeded_project}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_human_send_as_identity(client, seeded_project):
    """POST /mail/human/send sends message from chosen human identity."""
    # Register identity + create a recipient agent
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })

    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
        )).fetchone()
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid_row[0], "name": "SteelGuard", "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

    resp = await client.post("/mail/human/send", json={
        "project_slug": seeded_project,
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
    from mcp_agent_mail.db import get_session
    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT body_md, importance FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert "HUMAN OVERSEER" not in msg[0]
        assert msg[1] == "normal"


async def test_human_send_with_preamble(client, seeded_project):
    """POST /mail/human/send with include_preamble=True adds overseer preamble."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project,
        "name": "lee",
    })

    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
        )).fetchone()
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid_row[0], "name": "DeepWatch", "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

    resp = await client.post("/mail/human/send", json={
        "project_slug": seeded_project,
        "sender_name": "lee",
        "recipients": ["DeepWatch"],
        "subject": "Urgent directive",
        "body_md": "Drop everything.",
        "importance": "urgent",
        "include_preamble": True,
    })
    assert resp.status_code == 200
    data = resp.json()

    from mcp_agent_mail.db import get_session
    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT body_md, importance FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert "MESSAGE FROM HUMAN" in msg[0]
        assert msg[1] == "urgent"


async def test_human_send_validates_sender_is_human(client, seeded_project):
    """Cannot send as an AI agent identity."""
    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid_row = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
        )).fetchone()
        await session.execute(
            text("""INSERT OR IGNORE INTO agents
                    (project_id, name, program, model, task_description,
                     contact_policy, attachments_policy, inception_ts, last_active_ts)
                    VALUES (:pid, :name, 'claude-code', 'opus-4', 'test',
                            'open', 'auto', :ts, :ts)"""),
            {"pid": pid_row[0], "name": "FakeBot", "ts": datetime.now(timezone.utc)},
        )
        await session.commit()

    resp = await client.post("/mail/human/send", json={
        "project_slug": seeded_project,
        "sender_name": "FakeBot",
        "recipients": ["FakeBot"],
        "subject": "Spoofed",
        "body_md": "This should fail.",
    })
    assert resp.status_code == 403
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_human_identity.py -k "compose or send" -v`
Expected: FAIL

**Step 3: Implement compose page route and send route**

Add to `http.py`:

```python
@fastapi_app.get("/mail/human/compose")
async def human_compose(project: str | None = None) -> HTMLResponse:
    """Render compose page with identity selector."""
    await ensure_schema()
    async with get_session() as session:
        # Get all human identities with their projects
        id_rows = (
            await session.execute(
                text("""
                    SELECT a.id, a.name, a.task_description, p.slug, p.human_key
                    FROM agents a JOIN projects p ON a.project_id = p.id
                    WHERE a.model = 'Human'
                    ORDER BY a.name, p.slug
                """)
            )
        ).fetchall()

        identities = [
            {"name": r[1], "display_label": r[2], "project_slug": r[3], "project_path": r[4]}
            for r in id_rows
        ]

        # Get all agents grouped by project (for recipient selection)
        projects_agents: dict[str, list[dict]] = {}
        all_projects = (
            await session.execute(text("SELECT slug, human_key FROM projects ORDER BY slug"))
        ).fetchall()
        for prow in all_projects:
            agents = (
                await session.execute(
                    text("""
                        SELECT name, model FROM agents
                        WHERE project_id = (SELECT id FROM projects WHERE slug = :s)
                        ORDER BY name
                    """),
                    {"s": prow[0]},
                )
            ).fetchall()
            projects_agents[prow[0]] = [
                {"name": a[0], "is_human": a[1] == "Human"}
                for a in agents
            ]

    return await _render(
        "human_compose.html",
        identities=identities,
        projects_agents=projects_agents,
        all_projects=[{"slug": p[0], "path": p[1]} for p in all_projects],
        selected_project=project,
    )


@fastapi_app.post("/mail/human/send")
async def human_send(request: Request) -> JSONResponse:
    """Send message from a human identity. Validates sender is model=Human."""
    await ensure_schema()

    body = await request.json()
    project_slug: str = body.get("project_slug", "").strip()
    sender_name: str = body.get("sender_name", "").strip()
    recipients: list[str] = body.get("recipients", [])
    subject: str = body.get("subject", "").strip()
    body_md: str = body.get("body_md", "").strip()
    importance: str = body.get("importance", "normal")
    include_preamble: bool = body.get("include_preamble", False)
    thread_id: str | None = body.get("thread_id")

    # Validate inputs
    if not project_slug:
        raise HTTPException(status_code=400, detail="project_slug is required")
    if not sender_name:
        raise HTTPException(status_code=400, detail="sender_name is required")
    if not recipients:
        raise HTTPException(status_code=400, detail="At least one recipient required")
    if len(recipients) > 100:
        raise HTTPException(status_code=400, detail="Max 100 recipients")
    if not subject or len(subject) > 200:
        raise HTTPException(status_code=400, detail="Subject required (max 200 chars)")
    if not body_md or len(body_md) > 50000:
        raise HTTPException(status_code=400, detail="Body required (max 50,000 chars)")
    if importance not in ("low", "normal", "high", "urgent"):
        raise HTTPException(status_code=400, detail="Invalid importance level")

    recipients = list(dict.fromkeys(recipients))

    # Build body with optional preamble
    if include_preamble:
        preamble = """---

🚨 MESSAGE FROM HUMAN OPERATOR 🚨

This message is from a human operator. Please prioritize the instructions below.

---

"""
        full_body = preamble + body_md
    else:
        full_body = body_md

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Resolve project
        prow = (
            await session.execute(
                text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                {"k": project_slug},
            )
        ).fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail="Project not found")

        pid = int(prow[0])
        actual_slug = prow[1]

        # Validate sender is a human agent in this project
        sender_row = (
            await session.execute(
                text("SELECT id, model FROM agents WHERE project_id = :pid AND name = :name"),
                {"pid": pid, "name": sender_name},
            )
        ).fetchone()
        if not sender_row:
            raise HTTPException(status_code=404, detail=f"Sender '{sender_name}' not found in project")
        if sender_row[1] != "Human":
            raise HTTPException(status_code=403, detail="Cannot send as a non-human agent")

        sender_id = sender_row[0]

        # Insert message
        result = await session.execute(
            text("""
                INSERT INTO messages (project_id, sender_id, subject, body_md, importance, thread_id, created_ts, ack_required)
                VALUES (:pid, :sid, :subj, :body, :imp, :tid, :ts, 0)
                RETURNING id
            """),
            {"pid": pid, "sid": sender_id, "subj": subject, "body": full_body,
             "imp": importance, "tid": thread_id, "ts": now},
        )
        message_id = result.fetchone()[0]

        # Resolve recipients
        placeholders = ", ".join(f":name_{i}" for i in range(len(recipients)))
        params: dict[str, Any] = {"pid": pid}
        params.update({f"name_{i}": n for i, n in enumerate(recipients)})
        recipient_rows = (
            await session.execute(
                text(f"SELECT id, name FROM agents WHERE project_id = :pid AND name IN ({placeholders})"),
                params,
            )
        ).fetchall()
        recipient_map = {r[1]: r[0] for r in recipient_rows}
        valid_recipients = [n for n in recipients if n in recipient_map]

        if not valid_recipients:
            await session.rollback()
            raise HTTPException(status_code=400, detail="No valid recipients found")

        # Insert recipients
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            [{"mid": message_id, "aid": recipient_map[n]} for n in valid_recipients],
        )

        # Write to Git archive
        from .storage import ensure_archive, write_message_bundle
        settings = get_settings()
        archive = await ensure_archive(settings, actual_slug)
        await write_message_bundle(
            archive,
            {
                "id": message_id, "thread_id": thread_id,
                "project": prow[2], "project_slug": actual_slug,
                "from": sender_name, "to": valid_recipients,
                "cc": [], "bcc": [], "subject": subject,
                "importance": importance, "ack_required": False,
                "created": now.isoformat(), "attachments": [],
            },
            full_body, sender_name, valid_recipients,
            commit_text=f"{sender_name}: {subject}",
        )

        # Update sender activity
        await session.execute(
            text("UPDATE agents SET last_active_ts = :ts WHERE id = :id"),
            {"ts": now, "id": sender_id},
        )
        await session.commit()

    return JSONResponse({
        "success": True, "message_id": message_id,
        "sender": sender_name, "recipients": valid_recipients,
        "sent_at": now.isoformat(),
    })
```

**Step 4: Create compose template**

Create `src/mcp_agent_mail/templates/human_compose.html`:

This template extends `base.html` and provides:
- **Identity selector**: dropdown of registered human identities (name + project)
- **Project selector**: auto-filtered when identity is chosen
- **Recipient checkboxes**: filtered to agents in selected project (excluding sender)
- **Subject + body**: same Markdown editor as overseer_compose (toolbar, preview, split view)
- **Importance selector**: radio buttons for low/normal/high/urgent (default: normal)
- **Preamble toggle**: checkbox "Include priority preamble" (default: off)
- **Thread ID**: optional, for continuing conversations
- **Send confirmation modal**: shows summary before sending
- **Posts to**: `/mail/human/send`
- **On success**: redirects to `/mail/human/inbox`

Reuse all Markdown toolbar, preview, and toast patterns from `overseer_compose.html`.

**Step 5: Run tests**

Run: `python -m pytest tests/test_human_identity.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tests/test_human_identity.py src/mcp_agent_mail/http.py src/mcp_agent_mail/templates/human_compose.html
git commit -m "feat: add human compose with identity selection and importance control"
```

---

## Task 4: Reply-in-Thread from Human Identity

Let Lee reply to agent messages directly from the inbox, preserving thread context and using his human identity as the sender.

**Files:**
- Modify: `src/mcp_agent_mail/http.py` (add reply route + compose-reply route)
- Create: `src/mcp_agent_mail/templates/human_reply.html`

**Step 1: Write the failing test**

Add to `tests/test_human_identity.py`:

```python
async def _create_agent_message_to_lee(client, seeded_project):
    """Helper: register lee, create bot, send message to lee, return message_id."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    from datetime import datetime, timezone
    async with get_session() as session:
        pid = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :s"), {"s": seeded_project}
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


async def test_human_reply_page(client, seeded_project):
    """GET /mail/human/reply/{mid} renders reply composer pre-filled with thread context."""
    mid = await _create_agent_message_to_lee(client, seeded_project)
    resp = await client.get(f"/mail/human/reply/{mid}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_human_reply_sends_in_thread(client, seeded_project):
    """POST /mail/human/reply sends reply in same thread, addressed to original sender."""
    mid = await _create_agent_message_to_lee(client, seeded_project)
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
    from mcp_agent_mail.db import get_session
    from sqlalchemy import text
    async with get_session() as session:
        msg = (await session.execute(
            text("SELECT thread_id, subject FROM messages WHERE id = :mid"),
            {"mid": data["message_id"]},
        )).fetchone()
        assert msg[0] == "thread-42"
        assert msg[1].startswith("Re:")


async def test_human_reply_defaults_recipient_to_sender(client, seeded_project):
    """Reply auto-addresses to the original message sender."""
    mid = await _create_agent_message_to_lee(client, seeded_project)
    resp = await client.post("/mail/human/reply", json={
        "original_message_id": mid,
        "sender_name": "lee",
        "body_md": "Thanks.",
    })
    data = resp.json()
    assert "ReplyBot" in data["recipients"]
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_human_identity.py -k reply -v`
Expected: FAIL

**Step 3: Implement reply routes**

Add to `http.py`:

```python
@fastapi_app.get("/mail/human/reply/{message_id}")
async def human_reply_page(message_id: int) -> HTMLResponse:
    """Render reply composer pre-filled with thread context."""
    await ensure_schema()
    async with get_session() as session:
        # Get original message with sender info
        row = (
            await session.execute(
                text("""
                    SELECT m.id, m.subject, m.body_md, m.thread_id, m.importance,
                           m.created_ts, sender.name AS sender_name,
                           p.slug, p.human_key
                    FROM messages m
                    JOIN agents sender ON m.sender_id = sender.id
                    JOIN projects p ON m.project_id = p.id
                    WHERE m.id = :mid
                """),
                {"mid": message_id},
            )
        ).fetchone()
        if not row:
            return await _render("error.html", message="Message not found")

        # Get human identities in this project
        identities = (
            await session.execute(
                text("""
                    SELECT a.name, a.task_description
                    FROM agents a
                    JOIN projects p ON a.project_id = p.id
                    WHERE a.model = 'Human' AND p.slug = :slug
                """),
                {"slug": row[7]},
            )
        ).fetchall()

    original = {
        "id": row[0], "subject": row[1], "body_md": row[2],
        "thread_id": row[3] or str(row[0]), "importance": row[4],
        "created_ts": str(row[5]), "sender": row[6],
        "project_slug": row[7], "project_path": row[8],
    }
    reply_subject = row[1] if row[1].startswith("Re:") else f"Re: {row[1]}"

    return await _render(
        "human_reply.html",
        original=original,
        reply_subject=reply_subject,
        identities=[{"name": i[0], "display_label": i[1]} for i in identities],
    )


@fastapi_app.post("/mail/human/reply")
async def human_reply_send(request: Request) -> JSONResponse:
    """Send a reply from a human identity, inheriting thread context."""
    await ensure_schema()

    body = await request.json()
    original_mid: int = body.get("original_message_id", 0)
    sender_name: str = body.get("sender_name", "").strip()
    body_md: str = body.get("body_md", "").strip()
    importance: str = body.get("importance", "")
    extra_recipients: list[str] = body.get("extra_recipients", [])

    if not original_mid:
        raise HTTPException(status_code=400, detail="original_message_id required")
    if not sender_name:
        raise HTTPException(status_code=400, detail="sender_name required")
    if not body_md:
        raise HTTPException(status_code=400, detail="body_md required")

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Get original message
        orig = (
            await session.execute(
                text("""
                    SELECT m.id, m.subject, m.thread_id, m.importance, m.project_id,
                           sender.name AS sender_name, p.slug, p.human_key
                    FROM messages m
                    JOIN agents sender ON m.sender_id = sender.id
                    JOIN projects p ON m.project_id = p.id
                    WHERE m.id = :mid
                """),
                {"mid": original_mid},
            )
        ).fetchone()
        if not orig:
            raise HTTPException(status_code=404, detail="Original message not found")

        pid = orig[4]
        thread_id = orig[2] or str(orig[0])
        reply_subject = orig[1] if orig[1].startswith("Re:") else f"Re: {orig[1]}"
        reply_importance = importance if importance in ("low", "normal", "high", "urgent") else orig[3]

        # Validate sender is human in this project
        sender_row = (
            await session.execute(
                text("SELECT id, model FROM agents WHERE project_id = :pid AND name = :name"),
                {"pid": pid, "name": sender_name},
            )
        ).fetchone()
        if not sender_row or sender_row[1] != "Human":
            raise HTTPException(status_code=403, detail="Sender must be a registered human identity")

        # Default recipient = original sender
        recipients = [orig[5]] + extra_recipients
        recipients = list(dict.fromkeys(recipients))

        # Insert reply
        result = await session.execute(
            text("""
                INSERT INTO messages (project_id, sender_id, subject, body_md, importance, thread_id, created_ts, ack_required)
                VALUES (:pid, :sid, :subj, :body, :imp, :tid, :ts, 0)
                RETURNING id
            """),
            {"pid": pid, "sid": sender_row[0], "subj": reply_subject, "body": body_md,
             "imp": reply_importance, "tid": thread_id, "ts": now},
        )
        message_id = result.fetchone()[0]

        # Resolve recipients
        placeholders = ", ".join(f":name_{i}" for i in range(len(recipients)))
        params: dict[str, Any] = {"pid": pid}
        params.update({f"name_{i}": n for i, n in enumerate(recipients)})
        recipient_rows = (
            await session.execute(
                text(f"SELECT id, name FROM agents WHERE project_id = :pid AND name IN ({placeholders})"),
                params,
            )
        ).fetchall()
        recipient_map = {r[1]: r[0] for r in recipient_rows}
        valid_recipients = [n for n in recipients if n in recipient_map]

        if valid_recipients:
            await session.execute(
                text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
                [{"mid": message_id, "aid": recipient_map[n]} for n in valid_recipients],
            )

        # Write to Git archive
        from .storage import ensure_archive, write_message_bundle
        settings = get_settings()
        archive = await ensure_archive(settings, orig[6])
        await write_message_bundle(
            archive,
            {
                "id": message_id, "thread_id": thread_id,
                "project": orig[7], "project_slug": orig[6],
                "from": sender_name, "to": valid_recipients,
                "cc": [], "bcc": [], "subject": reply_subject,
                "importance": reply_importance, "ack_required": False,
                "created": now.isoformat(), "attachments": [],
            },
            body_md, sender_name, valid_recipients,
            commit_text=f"{sender_name} reply: {reply_subject}",
        )

        await session.execute(
            text("UPDATE agents SET last_active_ts = :ts WHERE id = :id"),
            {"ts": now, "id": sender_row[0]},
        )
        await session.commit()

    return JSONResponse({
        "success": True, "message_id": message_id,
        "subject": reply_subject, "sender": sender_name,
        "recipients": valid_recipients, "thread_id": thread_id,
    })
```

**Step 4: Create reply template**

Create `src/mcp_agent_mail/templates/human_reply.html`:

This template extends `base.html` and shows:
- **Quoted original message**: collapsible card showing sender, subject, body (rendered Markdown)
- **Thread context**: "Replying in thread: {thread_id}"
- **Identity selector**: dropdown (pre-selected if only one identity in project)
- **Reply body**: Markdown editor (same toolbar/preview as compose)
- **Pre-filled subject**: "Re: {original subject}"
- **Importance**: inherits from original, can override
- **Additional recipients**: optional, for CC'ing other agents
- **Posts to**: `/mail/human/reply`
- **On success**: redirects to `/mail/{project}/thread/{thread_id}`

**Step 5: Run tests**

Run: `python -m pytest tests/test_human_identity.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tests/test_human_identity.py src/mcp_agent_mail/http.py src/mcp_agent_mail/templates/human_reply.html
git commit -m "feat: add human reply-in-thread with inherited thread context"
```

---

## Task 5: Notes System

Let Lee write private notes attached to threads, agents, or projects — visible only to human identities, not delivered to agents.

**Files:**
- Modify: `src/mcp_agent_mail/models.py` (add Note model)
- Modify: `src/mcp_agent_mail/http.py` (add CRUD routes)
- Create: `src/mcp_agent_mail/templates/human_notes.html`

**Step 1: Write the failing test**

Add to `tests/test_human_identity.py`:

```python
async def test_create_note(client, seeded_project):
    """POST /mail/human/notes creates a private note."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    resp = await client.post("/mail/human/notes", json={
        "project_slug": seeded_project,
        "author": "lee",
        "body_md": "BrassAdama seems to be handling fleet ops well. Monitor for 48h.",
        "thread_id": "thread-42",
        "tags": ["observation", "fleet"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is not None
    assert data["author"] == "lee"


async def test_list_notes(client, seeded_project):
    """GET /mail/human/notes lists all notes, optionally filtered."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    await client.post("/mail/human/notes", json={
        "project_slug": seeded_project,
        "author": "lee",
        "body_md": "Note 1",
        "tags": ["fleet"],
    })
    await client.post("/mail/human/notes", json={
        "project_slug": seeded_project,
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


async def test_notes_not_visible_to_agents(client, seeded_project):
    """Notes should NOT appear in agent inboxes or unified inbox."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    await client.post("/mail/human/notes", json={
        "project_slug": seeded_project,
        "author": "lee",
        "body_md": "Secret observation",
    })

    # Check unified inbox — note should not appear
    resp = await client.get("/mail/api/unified-inbox")
    data = resp.json()
    for msg in data.get("messages", []):
        assert "Secret observation" not in msg.get("subject", "")
        assert "Secret observation" not in msg.get("excerpt", "")


async def test_delete_note(client, seeded_project):
    """DELETE /mail/human/notes/{id} removes a note."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    resp = await client.post("/mail/human/notes", json={
        "project_slug": seeded_project,
        "author": "lee",
        "body_md": "Temporary note",
    })
    note_id = resp.json()["id"]

    resp = await client.delete(f"/mail/human/notes/{note_id}")
    assert resp.status_code == 200

    resp = await client.get("/mail/human/notes/api")
    assert len(resp.json()["notes"]) == 0
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_human_identity.py -k note -v`
Expected: FAIL

**Step 3: Add Note model**

Add to `models.py`:

```python
class HumanNote(SQLModel, table=True):
    """Private notes written by human operators. NOT delivered to agents."""

    __tablename__ = "human_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    author_id: int = Field(foreign_key="agents.id", index=True)
    thread_id: Optional[str] = Field(default=None, index=True, max_length=128)
    body_md: str
    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    created_ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

**Step 4: Add schema migration**

In `db.py` `ensure_schema()`, add after existing CREATE TABLE statements:

```python
await session.execute(text("""
    CREATE TABLE IF NOT EXISTS human_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        author_id INTEGER NOT NULL REFERENCES agents(id),
        thread_id TEXT,
        body_md TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]',
        created_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""))
await session.execute(text(
    "CREATE INDEX IF NOT EXISTS idx_human_notes_project ON human_notes(project_id)"
))
await session.execute(text(
    "CREATE INDEX IF NOT EXISTS idx_human_notes_thread ON human_notes(thread_id)"
))
```

**Step 5: Implement notes routes**

Add to `http.py`:

```python
@fastapi_app.get("/mail/human/notes")
async def human_notes_page() -> HTMLResponse:
    """Render notes dashboard."""
    await ensure_schema()
    payload = await _build_notes_payload()
    return await _render("human_notes.html", **payload)


@fastapi_app.get("/mail/human/notes/api")
async def human_notes_api(
    tag: str | None = None,
    project: str | None = None,
    thread_id: str | None = None,
) -> JSONResponse:
    """JSON API for notes with optional filters."""
    await ensure_schema()
    payload = await _build_notes_payload(tag=tag, project=project, thread_id=thread_id)
    return JSONResponse(payload)


@fastapi_app.post("/mail/human/notes")
async def human_notes_create(request: Request) -> JSONResponse:
    """Create a private human note."""
    await ensure_schema()
    body = await request.json()
    project_slug: str = body.get("project_slug", "").strip()
    author: str = body.get("author", "").strip()
    body_md: str = body.get("body_md", "").strip()
    thread_id: str | None = body.get("thread_id")
    tags: list[str] = body.get("tags", [])

    if not project_slug or not author or not body_md:
        raise HTTPException(status_code=400, detail="project_slug, author, body_md required")

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        prow = (await session.execute(
            text("SELECT id FROM projects WHERE slug = :k OR human_key = :k"),
            {"k": project_slug},
        )).fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail="Project not found")

        author_row = (await session.execute(
            text("SELECT id, model FROM agents WHERE project_id = :pid AND name = :name"),
            {"pid": prow[0], "name": author},
        )).fetchone()
        if not author_row or author_row[1] != "Human":
            raise HTTPException(status_code=403, detail="Author must be a registered human identity")

        import json as json_mod
        result = await session.execute(
            text("""
                INSERT INTO human_notes (project_id, author_id, thread_id, body_md, tags, created_ts, updated_ts)
                VALUES (:pid, :aid, :tid, :body, :tags, :ts, :ts)
                RETURNING id
            """),
            {"pid": prow[0], "aid": author_row[0], "tid": thread_id,
             "body": body_md, "tags": json_mod.dumps(tags), "ts": now},
        )
        note_id = result.fetchone()[0]
        await session.commit()

    return JSONResponse({
        "id": note_id, "author": author, "project_slug": project_slug,
        "thread_id": thread_id, "tags": tags, "created_ts": now.isoformat(),
    })


@fastapi_app.delete("/mail/human/notes/{note_id}")
async def human_notes_delete(note_id: int) -> JSONResponse:
    """Delete a human note."""
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            text("DELETE FROM human_notes WHERE id = :id"), {"id": note_id}
        )
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Note not found")
    return JSONResponse({"success": True, "deleted": note_id})
```

Add `_build_notes_payload` helper:

```python
async def _build_notes_payload(
    *, tag: str | None = None, project: str | None = None, thread_id: str | None = None
) -> dict[str, Any]:
    """Fetch human notes with optional filters."""
    async with get_session() as session:
        query = """
            SELECT n.id, n.body_md, n.tags, n.thread_id, n.created_ts, n.updated_ts,
                   a.name AS author, p.slug AS project_slug, p.human_key
            FROM human_notes n
            JOIN agents a ON n.author_id = a.id
            JOIN projects p ON n.project_id = p.id
            WHERE 1=1
        """
        params: dict[str, Any] = {}
        if project:
            query += " AND p.slug = :proj"
            params["proj"] = project
        if thread_id:
            query += " AND n.thread_id = :tid"
            params["tid"] = thread_id
        query += " ORDER BY n.created_ts DESC LIMIT 500"

        rows = (await session.execute(text(query), params)).fetchall()

    import json as json_mod
    notes = []
    for r in rows:
        note_tags = json_mod.loads(r[2]) if isinstance(r[2], str) else (r[2] or [])
        if tag and tag not in note_tags:
            continue
        notes.append({
            "id": r[0], "body_md": r[1], "tags": note_tags,
            "thread_id": r[3], "created_ts": str(r[4]),
            "updated_ts": str(r[5]), "author": r[6],
            "project_slug": r[7], "project_path": r[8],
        })

    return {"notes": notes}
```

**Step 6: Create notes template**

Create `src/mcp_agent_mail/templates/human_notes.html`:

Dashboard extending `base.html` with:
- **Note composer**: inline Markdown editor at top (always visible)
- **Tag input**: comma-separated tags
- **Thread link**: optional, links note to a conversation thread
- **Notes feed**: reverse-chronological cards with rendered Markdown
- **Filter sidebar**: by tag, project, thread
- **Delete button**: per-note with confirmation
- **Empty state**: "No notes yet — start capturing observations"

**Step 7: Run tests**

Run: `python -m pytest tests/test_human_identity.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add src/mcp_agent_mail/models.py src/mcp_agent_mail/db.py src/mcp_agent_mail/http.py \
        src/mcp_agent_mail/templates/human_notes.html tests/test_human_identity.py
git commit -m "feat: add private human notes system with tags and thread linking"
```

---

## Task 6: Navigation — Wire It All Together

Add persistent navigation links for the human dashboard across the existing UI, and create a landing page that ties inbox, compose, and notes together.

**Files:**
- Modify: `src/mcp_agent_mail/templates/base.html` (add nav link)
- Modify: `src/mcp_agent_mail/http.py` (add landing route)
- Create: `src/mcp_agent_mail/templates/human_dashboard.html`

**Step 1: Write the failing test**

Add to `tests/test_human_identity.py`:

```python
async def test_human_dashboard(client, seeded_project):
    """GET /mail/human renders a dashboard landing page."""
    await client.post("/mail/human/register", json={
        "project_slug": seeded_project, "name": "lee",
    })
    resp = await client.get("/mail/human")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_base_template_has_human_link(client, seeded_project):
    """The unified inbox page should include a link to /mail/human."""
    resp = await client.get("/mail")
    assert resp.status_code == 200
    assert "/mail/human" in resp.text
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_human_identity.py -k dashboard -v`
Expected: FAIL

**Step 3: Add dashboard route**

Add to `http.py`:

```python
@fastapi_app.get("/mail/human")
async def human_dashboard() -> HTMLResponse:
    """Human operator dashboard — landing page for inbox, compose, notes."""
    await ensure_schema()
    inbox = await _build_human_inbox_payload(limit=10)
    notes = await _build_notes_payload()
    async with get_session() as session:
        id_rows = (await session.execute(
            text("""
                SELECT a.name, a.task_description, p.slug, p.human_key
                FROM agents a JOIN projects p ON a.project_id = p.id
                WHERE a.model = 'Human'
                ORDER BY a.name
            """)
        )).fetchall()
    identities = [
        {"name": r[0], "display_label": r[1], "project_slug": r[2], "project_path": r[3]}
        for r in id_rows
    ]
    return await _render(
        "human_dashboard.html",
        inbox=inbox, notes=notes, identities=identities,
    )
```

**Step 4: Create dashboard template**

Create `src/mcp_agent_mail/templates/human_dashboard.html`:

Extends `base.html`. Three-column layout:
- **Left**: Identity cards (registered human agents, with "Register in new project" button)
- **Center**: Recent inbox (last 10 messages, link to full inbox). Each has quick-reply button.
- **Right**: Recent notes (last 5, link to full notes). Inline note composer.
- **Header**: "Operator Dashboard" with compose button and unread badge
- **Quick actions**: Compose new message, View all threads, Search

**Step 5: Update base.html navigation**

Add a nav item to `base.html` in the header/navigation area. Look for the existing nav links and add:

```html
<a href="/mail/human" class="flex items-center gap-2 px-3 py-2 text-sm font-medium text-slate-600 dark:text-slate-400 hover:text-primary-600 dark:hover:text-primary-400 transition-colors">
  <i data-lucide="user" class="w-4 h-4"></i>
  Operator
</a>
```

**Step 6: Add reply button to message view**

Modify `mail_message.html` template to add a "Reply as Human" button when viewing a message that was sent to a human identity. The button links to `/mail/human/reply/{message_id}`.

**Step 7: Run tests**

Run: `python -m pytest tests/test_human_identity.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add src/mcp_agent_mail/templates/base.html src/mcp_agent_mail/templates/human_dashboard.html \
        src/mcp_agent_mail/templates/mail_message.html src/mcp_agent_mail/http.py \
        tests/test_human_identity.py
git commit -m "feat: add human operator dashboard and wire navigation"
```

---

## Task 7: Bootstrap Lee's Identity + Smoke Test

Register Lee's identity in all servitor pilot projects, send a test message, verify round-trip.

**Files:**
- No code changes — this is a manual integration test

**Step 1: Register Lee in all pilot projects**

Using the new API (or via the web UI at `/mail/human`):

```bash
# Register lee in each project via curl or the web UI
for project in servitor cass substack aiskills; do
  curl -s -X POST http://127.0.0.1:8765/mail/human/register \
    -H 'Content-Type: application/json' \
    -d "{\"project_slug\": \"$project\", \"name\": \"lee\", \"display_label\": \"Lee Gonzales\"}"
done
```

**Step 2: Verify identities are registered**

```bash
curl -s http://127.0.0.1:8765/mail/human/identities | python3 -m json.tool
```

Expected: 4 identities (one per project), all with `model=Human`

**Step 3: Send a test message from web UI**

1. Open `http://127.0.0.1:8765/mail/human/compose`
2. Select "lee" identity, select "servitor" project
3. Select "BrassAdama" as recipient
4. Subject: "Test: human-agent symmetry"
5. Body: "Testing the new compose system. Respond to confirm receipt."
6. Importance: normal
7. Preamble: OFF
8. Send

**Step 4: Verify in agent inbox**

```bash
curl -s "http://127.0.0.1:8765/mail/api/unified-inbox" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data['messages']:
    if 'symmetry' in m.get('subject', ''):
        print(f'OK: Message {m[\"id\"]} from {m[\"sender\"]} to {m[\"recipients\"]}')
        break
else:
    print('FAIL: Message not found')
"
```

**Step 5: Wait for servitor to wake and reply, then check human inbox**

Open `http://127.0.0.1:8765/mail/human/inbox` — should show BrassAdama's reply.

**Step 6: Reply to the agent**

Click the reply button on BrassAdama's message, write a response, send. Verify it appears in the thread view at `/mail/{project}/thread/{thread_id}`.

**Step 7: Create a note**

Open `http://127.0.0.1:8765/mail/human/notes`, write:
- Body: "Human-agent symmetry working. Full round-trip confirmed."
- Tags: integration-test, milestone
- Thread: (the thread_id from step 6)

**Step 8: Verify note is private**

Check that the unified inbox API does NOT include the note:

```bash
curl -s "http://127.0.0.1:8765/mail/api/unified-inbox" | grep -c "symmetry working"
# Expected: 0 (notes are private)
```

---

## Summary

| Task | What | Est. |
|------|------|------|
| 1 | Human identity registration (backend) | 45 min |
| 2 | Human inbox (backend + template) | 90 min |
| 3 | Human compose with identity selection | 90 min |
| 4 | Reply-in-thread from human identity | 60 min |
| 5 | Notes system (model + CRUD + template) | 90 min |
| 6 | Dashboard + navigation wiring | 60 min |
| 7 | Bootstrap identities + smoke test | 30 min |
| **Total** | | **~8 hours** |

**Zero schema changes to existing tables.** The Agent model already supports `model="Human"`. Only new addition is the `human_notes` table. All existing MCP tools, CLI commands, and agent workflows continue unchanged.
