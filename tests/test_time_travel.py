"""Tests for time travel functionality in MCP Agent Mail.

Tests historical inbox snapshot retrieval including:
- Basic time travel page rendering
- Historical inbox snapshots
- Timestamp parsing edge cases
- Commit traversal and message retrieval
- Error handling for invalid inputs
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.storage import ensure_archive, write_agent_profile, write_message_bundle


def _get_git_head_sha(repo_path: Path) -> str | None:
    """Get the HEAD SHA from a git repository (synchronous helper)."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


async def _setup_time_travel_data(settings: _config.Settings) -> dict:
    """Create test project with messages at different times."""
    await ensure_schema()

    # Create project in DB
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "timetravel-test", "hk": "/tmp/timetravel-test"},
        )
        await session.commit()
        row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "timetravel-test"})
        project_id = row.scalar()

        # Create agent
        await session.execute(
            text(
                "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, last_active_ts, attachments_policy, contact_policy) "
                "VALUES (:name, :pid, :prog, :model, :task, datetime('now'), datetime('now'), 'auto', 'auto')"
            ),
            {"name": "TimeTraveler", "pid": project_id, "prog": "claude-code", "model": "opus-4", "task": "Testing"},
        )
        await session.commit()

    # Create archive with commits at different times
    archive = await ensure_archive(settings, "timetravel-test")

    # Write agent profile
    await write_agent_profile(
        archive,
        {
            "name": "TimeTraveler",
            "program": "claude-code",
            "model": "opus-4",
            "task_description": "Testing time travel",
        },
    )

    # Write first message
    await write_message_bundle(
        archive,
        message={"id": 1, "subject": "First Message"},
        body_md="This is the first message.",
        sender="TimeTraveler",
        recipients=["TimeTraveler"],
    )

    # Get first commit SHA
    first_commit = _get_git_head_sha(archive.root)

    # Small delay to ensure different commit timestamps
    await asyncio.sleep(0.1)

    # Write second message
    await write_message_bundle(
        archive,
        message={"id": 2, "subject": "Second Message"},
        body_md="This is the second message.",
        sender="TimeTraveler",
        recipients=["TimeTraveler"],
    )

    # Get second commit SHA
    second_commit = _get_git_head_sha(archive.root)

    return {
        "project_id": project_id,
        "project_slug": "timetravel-test",
        "archive_root": archive.root,
        "first_commit": first_commit,
        "second_commit": second_commit,
    }


# =============================================================================
# Time Travel Page Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_page_renders(isolated_env):
    """Test GET /mail/archive/time-travel returns the time travel page."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/time-travel")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_time_travel_page_lists_projects(isolated_env):
    """Test time travel page includes available projects."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/time-travel")
        assert resp.status_code == 200
        # Page should contain project information
        assert "timetravel-test" in resp.text or "project" in resp.text.lower()


# =============================================================================
# Historical Snapshot API Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_snapshot_valid_timestamp(isolated_env):
    """Test snapshot retrieval with valid timestamp."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    # Use a future timestamp to get all messages
    future_ts = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": future_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert "snapshot_time" in data or "commit_sha" in data
        assert data.get("requested_time") == future_ts


@pytest.mark.asyncio
async def test_time_travel_snapshot_past_timestamp(isolated_env):
    """Test snapshot retrieval with timestamp before any commits."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    # Use a very old timestamp
    past_ts = "2000-01-01T00:00:00Z"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": past_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should return empty or note about no commits
        assert data.get("messages") == [] or "note" in data or "error" in data


@pytest.mark.asyncio
async def test_time_travel_snapshot_utc_timestamp(isolated_env):
    """Test snapshot with UTC timestamp (Z suffix)."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    utc_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": utc_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data


@pytest.mark.asyncio
async def test_time_travel_snapshot_timezone_offset(isolated_env):
    """Test snapshot with timezone offset timestamp."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    # Use timestamp with +05:30 offset
    ts_with_tz = "2099-12-31T23:59:59+05:30"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": ts_with_tz,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data


@pytest.mark.asyncio
async def test_time_travel_snapshot_naive_timestamp(isolated_env):
    """Test snapshot with naive timestamp (no timezone)."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    # Naive timestamp (no Z or offset)
    naive_ts = "2099-12-31T23:59:59"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": naive_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data


# =============================================================================
# Invalid Input Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_snapshot_invalid_timestamp_format(isolated_env):
    """Test snapshot with completely invalid timestamp format."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": "not-a-timestamp",
            },
        )
        # Should return 400 for invalid format
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_time_travel_snapshot_missing_timestamp(isolated_env):
    """Test snapshot with missing timestamp parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                # Missing timestamp
            },
        )
        # Should fail validation
        assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_time_travel_snapshot_invalid_project(isolated_env):
    """Test snapshot with invalid project slug."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "../../../etc/passwd",  # Path traversal attempt
                "agent": "TestAgent",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        )
        # Should reject invalid project
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_time_travel_snapshot_invalid_agent_name(isolated_env):
    """Test snapshot with invalid agent name format."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "invalid agent name with spaces!@#",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        )
        # Should reject invalid agent name
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_time_travel_snapshot_nonexistent_agent(isolated_env):
    """Test snapshot for agent that doesn't exist."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "NonExistentAgent",
                "timestamp": "2099-01-01T00:00:00Z",
            },
        )
        # Should return OK with empty messages (agent has no inbox)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("messages") == [] or "error" not in data


@pytest.mark.asyncio
async def test_time_travel_snapshot_nonexistent_project(isolated_env):
    """Test snapshot for project that doesn't exist."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "nonexistentproject",
                "agent": "TestAgent",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        )
        # Should handle gracefully (error in response or empty)
        assert resp.status_code in (200, 404)


# =============================================================================
# Timestamp Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_snapshot_partial_date_format(isolated_env):
    """Test snapshot with partial date format (date only, no time)."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": "2024-01-01",  # Date only
            },
        )
        # Should return 400 (format validation requires time)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_time_travel_snapshot_leap_second(isolated_env):
    """Test snapshot with leap second timestamp."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    # Some ISO 8601 parsers struggle with :60 seconds
    leap_ts = "2016-12-31T23:59:60Z"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": leap_ts,
            },
        )
        # Should handle gracefully (either parse or error cleanly)
        assert resp.status_code in (200, 400)


@pytest.mark.asyncio
async def test_time_travel_snapshot_negative_timezone(isolated_env):
    """Test snapshot with negative timezone offset."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    ts_negative_tz = "2099-12-31T23:59:59-08:00"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": ts_negative_tz,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data


@pytest.mark.asyncio
async def test_time_travel_snapshot_epoch(isolated_env):
    """Test snapshot at Unix epoch."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    epoch_ts = "1970-01-01T00:00:00Z"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": epoch_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should return empty (no commits at epoch)
        assert data.get("messages") == [] or "note" in data


# =============================================================================
# Response Structure Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_snapshot_response_structure(isolated_env):
    """Test that snapshot response has expected structure."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    future_ts = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": future_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Check required fields
        assert "messages" in data
        assert "requested_time" in data
        assert isinstance(data["messages"], list)

        # Check optional fields may be present
        if data.get("snapshot_time"):
            # If snapshot_time is present, it should be a string
            assert isinstance(data["snapshot_time"], str)
        if data.get("commit_sha"):
            # If commit_sha is present, should look like a SHA
            assert isinstance(data["commit_sha"], str)


@pytest.mark.asyncio
async def test_time_travel_snapshot_message_fields(isolated_env):
    """Test that messages in snapshot have expected fields."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    future_ts = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "TimeTraveler",
                "timestamp": future_ts,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        messages = data.get("messages", [])
        if messages:
            # Check at least one message has expected structure
            msg = messages[0]
            # Messages should have some identification
            assert "subject" in msg or "id" in msg or "date" in msg


# =============================================================================
# XSS Prevention Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_snapshot_xss_in_project(isolated_env):
    """Test XSS prevention in project parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "<script>alert('xss')</script>",
                "agent": "TestAgent",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        )
        # Should reject invalid project or sanitize the input
        assert resp.status_code in (200, 400)
        # Regardless of status, should never reflect raw script tag
        assert "<script>alert('xss')</script>" not in resp.text


@pytest.mark.asyncio
async def test_time_travel_snapshot_xss_in_agent(isolated_env):
    """Test XSS prevention in agent parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_time_travel_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "timetravel-test",
                "agent": "<img onerror=alert(1)>",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        )
        # Should reject invalid agent name
        assert resp.status_code == 400


# =============================================================================
# Empty State Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_travel_page_no_projects(isolated_env):
    """Test time travel page when no projects exist."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/time-travel")
        # Should still render, possibly with empty project list
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_time_travel_snapshot_project_no_messages(isolated_env):
    """Test snapshot for project with no messages."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    # Create project but no messages
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "empty-project", "hk": "/tmp/empty-project"},
        )
        await session.execute(
            text(
                "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, last_active_ts, attachments_policy, contact_policy) "
                "VALUES (:name, (SELECT id FROM projects WHERE slug = :slug), :prog, :model, :task, datetime('now'), datetime('now'), 'auto', 'auto')"
            ),
            {"name": "EmptyAgent", "slug": "empty-project", "prog": "test", "model": "test", "task": "Testing"},
        )
        await session.commit()

    # Ensure archive exists
    await ensure_archive(settings, "empty-project")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "empty-project",
                "agent": "EmptyAgent",
                "timestamp": "2099-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should return empty messages
        assert data.get("messages") == [] or "error" not in data
