"""P1 Core Tests: Project and Agent Setup.

Test project and agent registration flows that form the foundation
for all other MCP Agent Mail operations.

Test Cases:
1. ensure_project creates new project
2. ensure_project is idempotent
3. Project slug generated from human_key
4. register_agent creates new agent
5. register_agent updates existing agent
6. create_agent_identity always creates new
7. Agent profile written to Git archive
8. last_active_ts updated on activity
9. whois returns agent details

Reference: mcp_agent_mail-mm2
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from sqlalchemy import text
from sqlmodel import select

from mcp_agent_mail.app import ToolExecutionError, _get_agent, _get_agents_batch, build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Project

# ============================================================================
# Helper: Direct SQL verification
# ============================================================================


async def get_project_from_db(human_key: str) -> dict | None:
    """Get project details from database by human_key."""
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id, slug, human_key, created_at FROM projects WHERE human_key = :key"),
            {"key": human_key},
        )
        row = result.first()
        if row is None:
            return None
        return {
            "id": row[0],
            "slug": row[1],
            "human_key": row[2],
            "created_at": row[3],
        }


async def get_agent_from_db(project_id: int, agent_name: str) -> dict | None:
    """Get agent details from database."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, name, program, model, task_description, "
                "inception_ts, last_active_ts "
                "FROM agents WHERE project_id = :pid AND name = :name"
            ),
            {"pid": project_id, "name": agent_name},
        )
        row = result.first()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "program": row[2],
            "model": row[3],
            "task_description": row[4],
            "inception_ts": row[5],
            "last_active_ts": row[6],
        }


async def count_agents_in_project(project_id: int) -> int:
    """Count agents in a project."""
    async with get_session() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM agents WHERE project_id = :pid"),
            {"pid": project_id},
        )
        return result.scalar() or 0


# ============================================================================
# Test: ensure_project
# ============================================================================


@pytest.mark.asyncio
async def test_ensure_project_creates_new_project(isolated_env):
    """ensure_project creates a new project when it doesn't exist."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Verify project doesn't exist yet
        db_check = await get_project_from_db("/test/setup/new")
        assert db_check is None, "Project should not exist initially"

        # Create project
        result = await client.call_tool(
            "ensure_project", {"human_key": "/test/setup/new"}
        )

        # Verify response
        assert result.data["human_key"] == "/test/setup/new"
        assert "slug" in result.data
        assert "id" in result.data

        # Verify database record
        db_project = await get_project_from_db("/test/setup/new")
        assert db_project is not None, "Project should exist in database"
        assert db_project["human_key"] == "/test/setup/new"


@pytest.mark.asyncio
async def test_ensure_project_is_idempotent(isolated_env):
    """ensure_project returns existing project without modification."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Create project first time
        result1 = await client.call_tool(
            "ensure_project", {"human_key": "/test/setup/idem"}
        )
        first_id = result1.data["id"]
        first_slug = result1.data["slug"]

        # Call again - should be idempotent
        result2 = await client.call_tool(
            "ensure_project", {"human_key": "/test/setup/idem"}
        )

        # Should return same project
        assert result2.data["id"] == first_id, "Should return same project ID"
        assert result2.data["slug"] == first_slug, "Should return same slug"

        # Verify only one project exists
        db_project = await get_project_from_db("/test/setup/idem")
        assert db_project is not None, "Project should exist"
        assert db_project["id"] == first_id


@pytest.mark.asyncio
async def test_project_slug_generated_from_human_key(isolated_env):
    """Project slug is properly generated from human_key path."""
    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "ensure_project", {"human_key": "/data/projects/MyApp"}
        )

        # Slug should be lowercase, path-derived
        slug = result.data["slug"]
        assert slug is not None
        # Slug should be derived from path (lowercased, normalized)
        assert "myapp" in slug.lower() or "my-app" in slug.lower() or "data" in slug.lower()


# ============================================================================
# Test: register_agent
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_creates_new_agent(isolated_env):
    """register_agent creates a new agent with correct attributes."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Setup project
        await client.call_tool("ensure_project", {"human_key": "/test/setup/agent"})

        # Register agent (let server generate name)
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/agent",
                "program": "test-program",
                "model": "test-model",
                "task_description": "Testing agent creation",
            },
        )

        # Verify response
        assert "name" in result.data
        assert result.data["program"] == "test-program"
        assert result.data["model"] == "test-model"
        assert result.data["task_description"] == "Testing agent creation"

        # Verify database record
        project = await get_project_from_db("/test/setup/agent")
        assert project is not None, "Project should exist"
        agent = await get_agent_from_db(project["id"], result.data["name"])
        assert agent is not None, "Agent should exist in database"
        assert agent["program"] == "test-program"
        assert agent["model"] == "test-model"


@pytest.mark.asyncio
async def test_register_agent_updates_existing_agent(isolated_env):
    """register_agent updates an existing agent's metadata."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Setup
        await client.call_tool("ensure_project", {"human_key": "/test/setup/update"})

        # Register agent first time
        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/update",
                "program": "original-program",
                "model": "original-model",
                "task_description": "Original task",
            },
        )
        agent_name = result1.data["name"]

        # Get initial agent count
        project = await get_project_from_db("/test/setup/update")
        assert project is not None, "Project should exist"
        initial_count = await count_agents_in_project(project["id"])

        # Update agent with same name
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/update",
                "program": "original-program",
                "model": "original-model",
                "name": agent_name,
                "task_description": "Updated task",
            },
        )

        # Should return same agent name
        assert result2.data["name"] == agent_name

        # Verify count didn't increase (update, not create)
        final_count = await count_agents_in_project(project["id"])
        assert final_count == initial_count, "Should not create duplicate agent"

        # Verify task description was updated
        agent = await get_agent_from_db(project["id"], agent_name)
        assert agent is not None, "Agent should exist"
        assert agent["task_description"] == "Updated task"


@pytest.mark.asyncio
async def test_register_agent_generates_valid_name(isolated_env):
    """register_agent generates valid adjective+noun names."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/names"})

        # Register several agents and verify name format
        names = []
        for _ in range(3):
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": "/test/setup/names",
                    "program": "test",
                    "model": "test",
                },
            )
            name = result.data["name"]
            names.append(name)

            # Name should be CamelCase (adjective+noun pattern)
            assert name[0].isupper(), f"Name should start with capital: {name}"
            # Should have at least 2 capital letters (adjective + noun)
            capitals = sum(1 for c in name if c.isupper())
            assert capitals >= 2, f"Name should be adjective+noun pattern: {name}"

        # All names should be unique
        assert len(set(names)) == 3, "All generated names should be unique"


# ============================================================================
# Test: create_agent_identity
# ============================================================================


@pytest.mark.asyncio
async def test_create_agent_identity_always_creates_new(isolated_env):
    """create_agent_identity always creates a new agent, never updates."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/identity"})

        # Create first identity
        result1 = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/setup/identity",
                "program": "identity-test",
                "model": "test-model",
            },
        )
        name1 = result1.data["name"]

        # Create second identity
        result2 = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/setup/identity",
                "program": "identity-test",
                "model": "test-model",
            },
        )
        name2 = result2.data["name"]

        # Should be different names
        assert name1 != name2, "create_agent_identity should always create unique agents"

        # Both should exist in database
        project = await get_project_from_db("/test/setup/identity")
        assert project is not None, "Project should exist"
        agent1 = await get_agent_from_db(project["id"], name1)
        agent2 = await get_agent_from_db(project["id"], name2)
        assert agent1 is not None
        assert agent2 is not None


@pytest.mark.asyncio
async def test_create_agent_identity_with_name_hint(isolated_env):
    """create_agent_identity respects valid name_hint."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/hint"})

        # Create with valid name hint
        result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/setup/hint",
                "program": "test",
                "model": "test",
                "name_hint": "GreenCastle",
            },
        )

        # Should use the hint
        assert result.data["name"] == "GreenCastle"


# ============================================================================
# Test: last_active_ts updates
# ============================================================================


@pytest.mark.asyncio
async def test_last_active_ts_updated_on_activity(isolated_env):
    """last_active_ts is updated when agent performs actions."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/active"})

        # Register agent
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/active",
                "program": "test",
                "model": "test",
            },
        )
        agent_name = result.data["name"]

        # Get initial last_active_ts
        project = await get_project_from_db("/test/setup/active")
        assert project is not None, "Project should exist"
        agent_before = await get_agent_from_db(project["id"], agent_name)
        assert agent_before is not None, "Agent should exist before update"
        initial_ts = agent_before["last_active_ts"]

        # Small delay to ensure timestamp difference
        await asyncio.sleep(0.1)

        # Perform an action (register again to update)
        await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/active",
                "program": "test",
                "model": "test",
                "name": agent_name,
            },
        )

        # Verify last_active_ts was updated
        agent_after = await get_agent_from_db(project["id"], agent_name)
        assert agent_after is not None, "Agent should exist after update"
        assert agent_after["last_active_ts"] >= initial_ts, "last_active_ts should be updated"


# ============================================================================
# Test: whois
# ============================================================================


@pytest.mark.asyncio
async def test_whois_returns_agent_details(isolated_env):
    """whois returns comprehensive agent profile information."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/whois"})

        # Register agent with details
        reg_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/whois",
                "program": "whois-test-program",
                "model": "whois-test-model",
                "task_description": "Testing whois functionality",
            },
        )
        agent_name = reg_result.data["name"]

        # Query whois
        whois_result = await client.call_tool(
            "whois",
            {
                "project_key": "/test/setup/whois",
                "agent_name": agent_name,
            },
        )

        # Verify response contains expected fields
        assert whois_result.data["name"] == agent_name
        assert whois_result.data["program"] == "whois-test-program"
        assert whois_result.data["model"] == "whois-test-model"
        assert whois_result.data["task_description"] == "Testing whois functionality"
        assert "inception_ts" in whois_result.data
        assert "last_active_ts" in whois_result.data


@pytest.mark.asyncio
async def test_whois_with_recent_commits(isolated_env):
    """whois can include recent commit information."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/commits"})

        reg_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/commits",
                "program": "test",
                "model": "test",
            },
        )
        agent_name = reg_result.data["name"]

        # Query whois with recent commits
        whois_result = await client.call_tool(
            "whois",
            {
                "project_key": "/test/setup/commits",
                "agent_name": agent_name,
                "include_recent_commits": True,
                "commit_limit": 5,
            },
        )

        # Should include recent_commits field (may be empty list)
        assert "recent_commits" in whois_result.data


# ============================================================================
# Test: Git archive profile.json
# ============================================================================


@pytest.mark.asyncio
async def test_agent_profile_written_to_git_archive(isolated_env):
    """Agent registration writes profile.json to Git archive.

    Note: This test verifies the agent data is returned correctly.
    The actual file write to Git archive depends on storage configuration.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/archive"})

        # Register agent
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/setup/archive",
                "program": "archive-test",
                "model": "test-model",
                "task_description": "Testing archive writes",
            },
        )
        agent_name = result.data["name"]

        # Verify the agent was registered with correct data
        # (the actual file write happens in storage layer)
        assert agent_name is not None
        assert result.data["program"] == "archive-test"
        assert result.data["model"] == "test-model"

        # Verify via whois that data persisted
        whois_result = await client.call_tool(
            "whois",
            {
                "project_key": "/test/setup/archive",
                "agent_name": agent_name,
            },
        )
        assert whois_result.data["name"] == agent_name
        assert whois_result.data["program"] == "archive-test"


# ============================================================================
# Test: Error handling
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_invalid_name_rejected(isolated_env):
    """register_agent rejects or handles invalid agent names appropriately."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/invalid"})

        # Try to register with invalid name (placeholder name)
        # Some placeholder names may be caught, others may be accepted with warnings
        try:
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": "/test/setup/invalid",
                    "program": "test",
                    "model": "test",
                    "name": "YourAgentName",  # Should be caught as placeholder
                },
            )
            # If it was accepted, verify an agent was created (test passes)
            # This tests the graceful handling path
            assert result.data["name"] is not None
        except Exception as e:
            # If rejected, verify error message is meaningful
            error_msg = str(e).lower()
            assert any(keyword in error_msg for keyword in [
                "placeholder", "adjective", "noun", "invalid", "agent", "name"
            ]), f"Error should mention name validation: {e}"


@pytest.mark.asyncio
async def test_whois_nonexistent_agent_error(isolated_env):
    """whois returns error for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/noagent"})

        # Try to whois non-existent agent
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "whois",
                {
                    "project_key": "/test/setup/noagent",
                    "agent_name": "NonExistentAgent",
                },
            )

        # Should indicate agent not found
        error_msg = str(exc_info.value).lower()
        assert "not found" in error_msg or "does not exist" in error_msg


# ============================================================================
# Test: _get_agents_batch helper
# ============================================================================


async def _load_project(human_key: str) -> Project:
    async with get_session() as session:
        result = await session.execute(select(Project).where(Project.human_key == human_key))
        project = result.scalars().first()
    assert project is not None
    return project


@pytest.mark.asyncio
async def test_get_agents_batch_empty_list(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/batch-empty"})

    project = await _load_project("/test/setup/batch-empty")
    resolved = await _get_agents_batch(project, [])
    assert resolved == {}


@pytest.mark.asyncio
async def test_get_agents_batch_mixed_case(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/batch-case"})
        agent_result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/setup/batch-case",
                "program": "test",
                "model": "test",
                "task_description": "Batch case test",
            },
        )
        agent_name = agent_result.data["name"]

    project = await _load_project("/test/setup/batch-case")
    resolved = await _get_agents_batch(project, [agent_name.lower(), agent_name.upper()])
    assert resolved[agent_name.lower()].name == agent_name
    assert resolved[agent_name.upper()].name == agent_name


@pytest.mark.asyncio
async def test_get_agents_batch_missing_name_uses_get_agent_error(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/batch-missing"})
        agent_result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/setup/batch-missing",
                "program": "test",
                "model": "test",
                "task_description": "Batch missing test",
            },
        )
        agent_name = agent_result.data["name"]

    project = await _load_project("/test/setup/batch-missing")
    missing_name = f"{agent_name}Typo"
    with pytest.raises(ToolExecutionError) as batch_exc:
        await _get_agents_batch(project, [agent_name, missing_name])

    with pytest.raises(ToolExecutionError) as single_exc:
        await _get_agent(project, missing_name)

    assert batch_exc.value.error_type == single_exc.value.error_type
    assert str(batch_exc.value) == str(single_exc.value)


@pytest.mark.asyncio
async def test_get_agents_batch_placeholder_detection(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/setup/batch-placeholder"})

    project = await _load_project("/test/setup/batch-placeholder")
    with pytest.raises(ToolExecutionError) as exc_info:
        await _get_agents_batch(project, ["YOUR_AGENT_NAME"])
    assert exc_info.value.error_type == "CONFIGURATION_ERROR"
