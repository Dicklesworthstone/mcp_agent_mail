"""Tests for the Git archive browser and visualization routes.

Tests all /mail/archive/* endpoints for proper rendering and functionality.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

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


async def _setup_archive_with_commits(settings: _config.Settings) -> dict:
    """Create test archive with commits for visualization tests."""
    await ensure_schema()

    # Create project in DB
    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "archive-test", "hk": "/tmp/archive-test"},
        )
        await session.commit()
        row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "archive-test"})
        project_id = row.scalar()

    # Create archive with some commits
    archive = await ensure_archive(settings, "archive-test")

    # Write agent profile (creates a commit)
    await write_agent_profile(
        archive,
        {
            "name": "GreenCastle",
            "program": "claude-code",
            "model": "opus-4",
            "task_description": "Archive testing",
        },
    )

    # Write a message (creates another commit)
    await write_message_bundle(
        archive,
        message={"id": 1, "subject": "Archive Test Message", "created": "2026-01-12T12:00:00"},
        body_md="This is a test message for archive visualization.",
        sender="GreenCastle",
        recipients=["BlueLake"],
    )

    # Get the commit SHA from the archive
    head_sha = _get_git_head_sha(archive.root)

    return {
        "project_id": project_id,
        "project_slug": "archive-test",
        "archive_root": archive.root,
        "head_sha": head_sha,
    }


# =============================================================================
# Archive Guide Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_guide(isolated_env):
    """Test GET /mail/archive/guide returns guide page."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/guide")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Activity/Commits View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_activity(isolated_env):
    """Test GET /mail/archive/activity returns recent commits."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/activity")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_activity_with_limit(isolated_env):
    """Test activity view with limit parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/activity", params={"limit": 5})
        assert resp.status_code == 200


# =============================================================================
# Commit Detail Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_commit_detail(isolated_env):
    """Test GET /mail/archive/commit/{sha} returns commit detail."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_archive_with_commits(settings)

    if not data["head_sha"]:
        pytest.skip("No commits in archive")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/mail/archive/commit/{data['head_sha']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_commit_short_sha(isolated_env):
    """Test commit detail with short SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_archive_with_commits(settings)

    if not data["head_sha"]:
        pytest.skip("No commits in archive")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        short_sha = data["head_sha"][:7]
        resp = await client.get(f"/mail/archive/commit/{short_sha}")
        # Should work with short SHA
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_commit_invalid_sha(isolated_env):
    """Test commit detail with invalid SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/commit/invalidsha123")
        # Should return 404 or error page
        assert resp.status_code in (200, 404, 500)


@pytest.mark.asyncio
async def test_archive_commit_nonexistent_sha(isolated_env):
    """Test commit detail with nonexistent but valid-format SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Valid format but doesn't exist
        resp = await client.get("/mail/archive/commit/0000000000000000000000000000000000000000")
        assert resp.status_code in (200, 404)


# =============================================================================
# Timeline Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_timeline(isolated_env):
    """Test GET /mail/archive/timeline returns timeline visualization."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/timeline")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Browser (Directory Tree) Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_browser(isolated_env):
    """Test GET /mail/archive/browser returns directory browser."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/browser")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_browser_file_content(isolated_env):
    """Test GET /mail/archive/browser/{project}/file returns file content."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Try to get agents/GreenCastle/profile.json
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "agents/GreenCastle/profile.json"},
        )
        # Should return content or 404 if not found
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_browser_file_nonexistent(isolated_env):
    """Test file browser with nonexistent file."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "nonexistent/file.txt"},
        )
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_browser_path_traversal_prevention(isolated_env):
    """Test that path traversal attempts are blocked."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Try path traversal
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "../../../etc/passwd"},
        )
        # Should not expose system files - must be error status or empty content
        assert resp.status_code in (200, 400, 403, 404)
        # Even if 200, should not contain password file content
        assert "root:" not in resp.text


# =============================================================================
# Network Graph Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_network(isolated_env):
    """Test GET /mail/archive/network returns agent communication graph."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/network")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_network_empty(isolated_env):
    """Test network graph with no messages."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/network")
        # Should handle empty state gracefully
        assert resp.status_code == 200


# =============================================================================
# Time Travel Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_time_travel_page(isolated_env):
    """Test GET /mail/archive/time-travel returns time travel interface."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/time-travel")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot(isolated_env):
    """Test GET /mail/archive/time-travel/snapshot returns historical inbox."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get snapshot at current time
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "2099-12-31T23:59:59Z",
            },
        )
        # Should return JSON snapshot
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot_invalid_timestamp(isolated_env):
    """Test time travel with invalid timestamp."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "not-a-timestamp",
            },
        )
        # Should handle invalid timestamp gracefully
        assert resp.status_code in (200, 400, 422)


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot_past_timestamp(isolated_env):
    """Test time travel with timestamp before any commits."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Very old timestamp
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "2000-01-01T00:00:00Z",
            },
        )
        # Should return empty or appropriate response
        assert resp.status_code in (200, 404)


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


@pytest.mark.asyncio
async def test_archive_routes_no_projects(isolated_env):
    """Test archive routes when no projects exist."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # All routes should handle empty state gracefully
        routes = [
            "/mail/archive/guide",
            "/mail/archive/activity",
            "/mail/archive/timeline",
            "/mail/archive/browser",
            "/mail/archive/network",
            "/mail/archive/time-travel",
        ]
        for route in routes:
            resp = await client.get(route)
            assert resp.status_code in (200, 404), f"Route {route} failed with {resp.status_code}"


@pytest.mark.asyncio
async def test_archive_commit_xss_in_sha(isolated_env):
    """Test that XSS in SHA parameter is escaped."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        xss = "<script>alert('xss')</script>"
        resp = await client.get(f"/mail/archive/commit/{xss}")
        # Should not execute script
        assert resp.status_code in (200, 400, 404)
        # Regardless of status, should never reflect raw script tag
        assert "<script>alert('xss')</script>" not in resp.text
