"""Tests for human identity registration and management."""

from __future__ import annotations

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
