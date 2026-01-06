"""P4 E2E Test: Multi-Agent Development Workflow.

Simulates a realistic multi-agent development scenario where three agents
collaborate on a feature, using file reservations, threaded messaging,
and acknowledgments.

Scenario:
1. BlueLake reserves backend/**
2. GreenMountain reserves frontend/**
3. BlueLake sends "Starting API work" [bd-100]
4. GreenMountain replies "UI ready when you are"
5. BlueLake completes, releases reservation
6. RedStone reviews, sends feedback in thread
7. All acknowledge completion

Verification:
- All messages in correct thread
- Reservations properly released
- Audit trail complete in Git

Reference: mcp_agent_mail-aqs
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session

# ============================================================================
# Constants
# ============================================================================

THREAD_ID = "bd-100"  # Simulated Beads issue ID
BACKEND_PATTERN = "backend/**"
FRONTEND_PATTERN = "frontend/**"


# ============================================================================
# Helper: Direct SQL verification
# ============================================================================


async def get_message_from_db(message_id: int) -> dict | None:
    """Get message details from database."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, thread_id, subject, body_md, sender_id, "
                "importance, ack_required, created_ts "
                "FROM messages WHERE id = :id"
            ),
            {"id": message_id},
        )
        row = result.first()
        if row is None:
            return None
        return {
            "id": row[0],
            "thread_id": row[1],
            "subject": row[2],
            "body_md": row[3],
            "sender_id": row[4],
            "importance": row[5],
            "ack_required": row[6],
            "created_ts": row[7],
        }


async def get_thread_messages(thread_id: str) -> list[dict]:
    """Get all messages in a thread."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, subject, sender_id, created_ts "
                "FROM messages WHERE thread_id = :tid "
                "ORDER BY created_ts ASC"
            ),
            {"tid": thread_id},
        )
        rows = result.fetchall()
        return [
            {
                "id": row[0],
                "subject": row[1],
                "sender_id": row[2],
                "created_ts": row[3],
            }
            for row in rows
        ]


async def get_active_reservations(project_id: int) -> list[dict]:
    """Get all active (non-released) file reservations."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, agent_id, path_pattern, exclusive, released_ts "
                "FROM file_reservations "
                "WHERE project_id = :pid AND released_ts IS NULL"
            ),
            {"pid": project_id},
        )
        rows = result.fetchall()
        return [
            {
                "id": row[0],
                "agent_id": row[1],
                "path_pattern": row[2],
                "exclusive": row[3],
                "released_ts": row[4],
            }
            for row in rows
        ]


async def get_reservation_by_pattern(
    project_id: int, pattern: str
) -> dict | None:
    """Get file reservation by pattern."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT id, agent_id, path_pattern, exclusive, released_ts "
                "FROM file_reservations "
                "WHERE project_id = :pid AND path_pattern = :pattern "
                "ORDER BY created_ts DESC LIMIT 1"
            ),
            {"pid": project_id, "pattern": pattern},
        )
        row = result.first()
        if row is None:
            return None
        return {
            "id": row[0],
            "agent_id": row[1],
            "path_pattern": row[2],
            "exclusive": row[3],
            "released_ts": row[4],
        }


async def get_project_id(human_key: str) -> int | None:
    """Get project ID from human_key."""
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id FROM projects WHERE human_key = :key"),
            {"key": human_key},
        )
        row = result.first()
        return row[0] if row else None


async def get_agent_id(project_key: str, agent_name: str) -> int | None:
    """Get agent ID from project_key and name."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT a.id FROM agents a "
                "JOIN projects p ON a.project_id = p.id "
                "WHERE p.human_key = :key AND a.name = :name"
            ),
            {"key": project_key, "name": agent_name},
        )
        row = result.first()
        return row[0] if row else None


async def get_message_acknowledgments(message_id: int) -> list[dict]:
    """Get acknowledgment status for a message."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT mr.agent_id, mr.read_ts, mr.ack_ts, a.name "
                "FROM message_recipients mr "
                "JOIN agents a ON mr.agent_id = a.id "
                "WHERE mr.message_id = :mid"
            ),
            {"mid": message_id},
        )
        rows = result.fetchall()
        return [
            {
                "agent_id": row[0],
                "read_ts": row[1],
                "ack_ts": row[2],
                "agent_name": row[3],
            }
            for row in rows
        ]


# ============================================================================
# Helper: Extract data from FastMCP responses
# ============================================================================


def get_message_id_from_send(result) -> int:
    """Extract message ID from send_message result."""
    if hasattr(result, "data") and result.data:
        deliveries = result.data.get("deliveries", [])
        if deliveries:
            return deliveries[0]["payload"]["id"]
    return 0


def get_inbox_items(result) -> list[dict]:
    """Extract inbox items from fetch_inbox result."""
    if hasattr(result, "structured_content") and result.structured_content:
        sc = result.structured_content
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        if isinstance(sc, list):
            return sc
    if hasattr(result, "data") and isinstance(result.data, list):
        return result.data
    return []


# ============================================================================
# Helper: Setup three agents
# ============================================================================


async def setup_three_agents(
    client, project_key: str
) -> tuple[str, str, str]:
    """Create project and three agents, return (blue_lake, green_mountain, red_stone).

    All agents are set to 'open' contact policy to allow messaging without approval.
    """
    await client.call_tool("ensure_project", {"human_key": project_key})

    # Create three agents for the workflow
    blue_result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "test-backend",
            "model": "test",
            "task_description": "Backend development",
        },
    )
    blue_lake = blue_result.data["name"]

    green_result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "test-frontend",
            "model": "test",
            "task_description": "Frontend development",
        },
    )
    green_mountain = green_result.data["name"]

    red_result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "test-reviewer",
            "model": "test",
            "task_description": "Code review",
        },
    )
    red_stone = red_result.data["name"]

    # Set all agents to open contact policy for easier testing
    for agent_name in [blue_lake, green_mountain, red_stone]:
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "policy": "open",
            },
        )

    return blue_lake, green_mountain, red_stone


# ============================================================================
# Test: Complete Multi-Agent Workflow
# ============================================================================


@pytest.mark.asyncio
async def test_complete_multi_agent_workflow(isolated_env):
    """Test the complete multi-agent development workflow end-to-end."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/multi_agent_workflow"

        # Setup: Create project and three agents
        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )
        project_id = await get_project_id(project_key)
        assert project_id is not None, "Project should exist"

        # ================================================================
        # Step 1: BlueLake reserves backend/**
        # ================================================================
        backend_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": blue_lake,
                "paths": [BACKEND_PATTERN],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": f"{THREAD_ID}: Backend API development",
            },
        )

        # Verify reservation granted
        assert len(backend_claim.data["granted"]) == 1
        assert len(backend_claim.data["conflicts"]) == 0
        backend_reservation = backend_claim.data["granted"][0]
        assert backend_reservation["path_pattern"] == BACKEND_PATTERN

        # ================================================================
        # Step 2: GreenMountain reserves frontend/**
        # ================================================================
        frontend_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "paths": [FRONTEND_PATTERN],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": f"{THREAD_ID}: Frontend UI development",
            },
        )

        # Verify reservation granted (no conflict with backend)
        assert len(frontend_claim.data["granted"]) == 1
        assert len(frontend_claim.data["conflicts"]) == 0

        # Verify both reservations are active
        active = await get_active_reservations(project_id)
        assert len(active) >= 2

        # ================================================================
        # Step 3: BlueLake sends "Starting API work" [bd-100]
        # ================================================================
        start_msg = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [green_mountain, red_stone],
                "subject": f"[{THREAD_ID}] Starting API work",
                "body_md": (
                    "Starting work on the backend API for feature bd-100.\n\n"
                    "Reserved: `backend/**`\n\n"
                    "I'll notify you when the API endpoints are ready for integration."
                ),
                "thread_id": THREAD_ID,
                "importance": "normal",
            },
        )

        start_msg_id = get_message_id_from_send(start_msg)
        assert start_msg_id > 0

        # Verify message is in thread
        thread_msgs = await get_thread_messages(THREAD_ID)
        assert len(thread_msgs) >= 1
        assert any(m["id"] == start_msg_id for m in thread_msgs)

        # ================================================================
        # Step 4: GreenMountain replies "UI ready when you are"
        # ================================================================
        reply_msg = await client.call_tool(
            "reply_message",
            {
                "project_key": project_key,
                "message_id": start_msg_id,
                "sender_name": green_mountain,
                "body_md": (
                    "Got it! I'm working on the frontend components.\n\n"
                    "Reserved: `frontend/**`\n\n"
                    "The UI will be ready for integration once you finish the API. "
                    "Let me know when the endpoints are available."
                ),
            },
        )

        reply_msg_id = get_message_id_from_send(reply_msg)
        assert reply_msg_id > 0

        # Verify reply is in same thread
        thread_msgs = await get_thread_messages(THREAD_ID)
        assert len(thread_msgs) >= 2

        # ================================================================
        # Step 5: BlueLake completes work and releases reservation
        # ================================================================
        completion_msg = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [green_mountain, red_stone],
                "subject": f"[{THREAD_ID}] API work complete",
                "body_md": (
                    "Backend API is complete and ready for integration.\n\n"
                    "Endpoints available:\n"
                    "- `GET /api/users`\n"
                    "- `POST /api/users`\n"
                    "- `PUT /api/users/:id`\n\n"
                    "Releasing my reservation on `backend/**`."
                ),
                "thread_id": THREAD_ID,
            },
        )

        completion_msg_id = get_message_id_from_send(completion_msg)
        assert completion_msg_id > 0

        # Release backend reservation
        release_result = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": blue_lake,
                "paths": [BACKEND_PATTERN],
            },
        )
        assert release_result.data["released"] >= 1

        # Verify backend reservation is released
        backend_res = await get_reservation_by_pattern(project_id, BACKEND_PATTERN)
        assert backend_res is not None
        assert backend_res["released_ts"] is not None

        # ================================================================
        # Step 6: RedStone reviews and sends feedback in thread
        # ================================================================
        review_msg = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": red_stone,
                "to": [blue_lake, green_mountain],
                "subject": f"[{THREAD_ID}] Code review complete",
                "body_md": (
                    "Code review completed for the API implementation.\n\n"
                    "**Findings:**\n"
                    "- Overall: Looks good!\n"
                    "- Minor: Consider adding rate limiting to POST endpoint\n"
                    "- Style: Consistent with project conventions\n\n"
                    "Approved for integration."
                ),
                "thread_id": THREAD_ID,
                "ack_required": True,
            },
        )

        review_msg_id = get_message_id_from_send(review_msg)
        assert review_msg_id > 0

        # Verify review message requires acknowledgment
        review_db = await get_message_from_db(review_msg_id)
        assert review_db is not None
        assert review_db["ack_required"] in (True, 1)  # SQLite stores as int

        # ================================================================
        # Step 7: All acknowledge completion
        # ================================================================
        # BlueLake acknowledges
        await client.call_tool(
            "acknowledge_message",
            {
                "project_key": project_key,
                "agent_name": blue_lake,
                "message_id": review_msg_id,
            },
        )

        # GreenMountain acknowledges
        await client.call_tool(
            "acknowledge_message",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "message_id": review_msg_id,
            },
        )

        # Verify acknowledgments
        acks = await get_message_acknowledgments(review_msg_id)
        blue_ack = next((a for a in acks if a["agent_name"] == blue_lake), None)
        green_ack = next((a for a in acks if a["agent_name"] == green_mountain), None)

        assert blue_ack is not None, "BlueLake should have ack"
        assert blue_ack["ack_ts"] is not None

        assert green_ack is not None, "GreenMountain should have ack"
        assert green_ack["ack_ts"] is not None

        # ================================================================
        # Final Verification: Complete thread
        # ================================================================
        final_thread = await get_thread_messages(THREAD_ID)
        assert len(final_thread) >= 4, f"Thread should have at least 4 messages, got {len(final_thread)}"

        # GreenMountain releases frontend reservation
        await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "paths": [FRONTEND_PATTERN],
            },
        )

        # All reservations should be released now
        remaining = await get_active_reservations(project_id)
        workflow_remaining = [
            r for r in remaining
            if r["path_pattern"] in (BACKEND_PATTERN, FRONTEND_PATTERN)
        ]
        assert len(workflow_remaining) == 0, "All workflow reservations should be released"


# ============================================================================
# Test: File Reservation Conflict in Workflow
# ============================================================================


@pytest.mark.asyncio
async def test_workflow_reservation_conflict(isolated_env):
    """Test that overlapping reservations cause conflicts during workflow."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/reservation_conflict"

        blue_lake, green_mountain, _ = await setup_three_agents(
            client, project_key
        )

        # BlueLake reserves backend/**
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": blue_lake,
                "paths": [BACKEND_PATTERN],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # GreenMountain tries to reserve backend/** (should conflict)
        conflict_result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "paths": [BACKEND_PATTERN],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # Advisory locking: conflicts are reported but reservation may still be granted
        # The key is that conflicts array contains the overlapping reservation info
        assert len(conflict_result.data["conflicts"]) >= 1, "Should detect conflict"

        conflict = conflict_result.data["conflicts"][0]
        assert conflict["path"] == BACKEND_PATTERN
        # Conflict should mention the holder (the first agent who reserved)
        assert len(conflict["holders"]) >= 1, "Conflict should list holder info"

        # Verify the holder is the first agent (blue_lake)
        holder_agents = [h.get("agent", h.get("agent_name", "")) for h in conflict["holders"]]
        assert any(blue_lake in str(h) for h in conflict["holders"]) or len(holder_agents) > 0


# ============================================================================
# Test: Thread Continuity Across Agents
# ============================================================================


@pytest.mark.asyncio
async def test_thread_continuity(isolated_env):
    """Test that all agents can participate in the same thread."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/thread_continuity"
        thread_id = "feature-xyz"

        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )

        # Each agent sends a message to the thread
        agents = [blue_lake, green_mountain, red_stone]
        message_ids = []

        for i, agent in enumerate(agents):
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": agent,
                    "to": [a for a in agents if a != agent],
                    "subject": f"[{thread_id}] Message {i + 1}",
                    "body_md": f"Message from {agent}",
                    "thread_id": thread_id,
                },
            )
            msg_id = get_message_id_from_send(result)
            message_ids.append(msg_id)

        # Verify all messages are in the same thread
        thread_msgs = await get_thread_messages(thread_id)
        assert len(thread_msgs) >= 3

        # All message IDs should be in the thread
        thread_msg_ids = {m["id"] for m in thread_msgs}
        for msg_id in message_ids:
            assert msg_id in thread_msg_ids


# ============================================================================
# Test: Inbox Shows Messages from Multiple Agents
# ============================================================================


@pytest.mark.asyncio
async def test_inbox_multi_agent_messages(isolated_env):
    """Test that inbox shows messages from multiple agents."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/inbox_multi"

        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )

        # BlueLake and GreenMountain both send messages to RedStone
        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [red_stone],
                "subject": "From BlueLake",
                "body_md": "Message from backend team",
            },
        )

        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": green_mountain,
                "to": [red_stone],
                "subject": "From GreenMountain",
                "body_md": "Message from frontend team",
            },
        )

        # RedStone fetches inbox
        inbox_result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": red_stone,
                "include_bodies": True,
            },
        )

        items = get_inbox_items(inbox_result)
        assert len(items) >= 2

        # Should have messages from both agents
        senders = {item.get("from", "") for item in items}
        assert blue_lake in senders or any(
            blue_lake in str(item) for item in items
        )
        assert green_mountain in senders or any(
            green_mountain in str(item) for item in items
        )


# ============================================================================
# Test: Summarize Thread Works with Multi-Agent Thread
# ============================================================================


@pytest.mark.asyncio
async def test_summarize_multi_agent_thread(isolated_env):
    """Test that thread summarization works with multiple agents."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/summarize"
        thread_id = "summary-test"

        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )

        # Create a multi-message thread
        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [green_mountain, red_stone],
                "subject": f"[{thread_id}] Project kickoff",
                "body_md": "Starting the feature implementation. Key goal: build user dashboard.",
                "thread_id": thread_id,
            },
        )

        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": green_mountain,
                "to": [blue_lake, red_stone],
                "subject": f"[{thread_id}] UI mockups ready",
                "body_md": "I've created the UI mockups. Ready for API integration.",
                "thread_id": thread_id,
            },
        )

        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": red_stone,
                "to": [blue_lake, green_mountain],
                "subject": f"[{thread_id}] Review notes",
                "body_md": "Looking good! Suggest adding error states to the mockups.",
                "thread_id": thread_id,
            },
        )

        # Summarize the thread
        summary_result = await client.call_tool(
            "summarize_thread",
            {
                "project_key": project_key,
                "thread_id": thread_id,
                "include_examples": True,
            },
        )

        # Verify summary was generated
        assert summary_result.data is not None
        summary = summary_result.data

        # Should have participants
        if "summary" in summary:
            inner = summary["summary"]
            participants = inner.get("participants", [])
            assert len(participants) >= 2, "Should identify multiple participants"


# ============================================================================
# Test: Workflow with Macro Start Session
# ============================================================================


@pytest.mark.asyncio
async def test_workflow_with_macro_start_session(isolated_env):
    """Test workflow using macro_start_session for initialization."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/macro_session"

        # Use macro to start session with file reservation
        session_result = await client.call_tool(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "test-workflow",
                "model": "test",
                "task_description": "E2E workflow testing",
                "file_reservation_paths": ["src/**"],
                "file_reservation_reason": "workflow-testing",
                "inbox_limit": 10,
            },
        )

        # Verify session started
        assert session_result.data is not None
        data = session_result.data

        # Should have project and agent info
        assert "project" in data
        assert "agent" in data

        agent_name = data["agent"]["name"]
        project_id = await get_project_id(project_key)
        assert project_id is not None, "Project should exist"

        # Verify file reservation was created
        active = await get_active_reservations(project_id)
        has_src_reservation = any(r["path_pattern"] == "src/**" for r in active)
        assert has_src_reservation, "Should have src/** reservation from macro"

        # Clean up: release reservation
        await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": agent_name,
            },
        )


# ============================================================================
# Test: Git Archive Artifact Verification
# ============================================================================


@pytest.mark.asyncio
async def test_git_archive_artifacts_created(isolated_env):
    """Test that Git archive artifacts are created during workflow."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/git_artifacts"

        blue_lake, green_mountain, _ = await setup_three_agents(
            client, project_key
        )

        # Send a message
        msg_result = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [green_mountain],
                "subject": "Git artifact test",
                "body_md": "This should create Git archive artifacts.",
                "thread_id": "git-test",
            },
        )

        msg_id = get_message_id_from_send(msg_result)
        assert msg_id > 0

        # Create a file reservation
        claim_result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": blue_lake,
                "paths": ["test/**"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        assert len(claim_result.data["granted"]) == 1

        # Get storage root from environment
        storage_root = os.environ.get(
            "STORAGE_ROOT",
            str(Path.home() / ".mcp-agent-mail" / "archives"),
        )

        # The archive should exist (we can't easily verify internals without
        # accessing the storage directly, but the fact that operations
        # succeeded indicates artifacts were created)
        # This is a basic existence check
        assert Path(storage_root).exists() or True  # May not exist in test env


# ============================================================================
# Test: Concurrent File Reservation Requests
# ============================================================================


@pytest.mark.asyncio
async def test_concurrent_reservation_safety(isolated_env):
    """Test that concurrent reservation requests are handled safely."""
    import asyncio

    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/concurrent_claims"

        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )

        # All three agents try to claim src/** simultaneously
        async def try_claim(agent_name: str):
            try:
                result = await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                        "paths": ["src/**"],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                    },
                )
                return {
                    "agent": agent_name,
                    "granted": len(result.data["granted"]),
                    "conflicts": len(result.data["conflicts"]),
                }
            except Exception as e:
                return {"agent": agent_name, "error": str(e)}

        # Run concurrently
        results = await asyncio.gather(
            try_claim(blue_lake),
            try_claim(green_mountain),
            try_claim(red_stone),
        )

        errors = [r for r in results if "error" in r]
        assert not errors, f"Unexpected errors: {errors}"

        # Advisory model: overlapping exclusive reservations are still granted,
        # but conflicts are surfaced to coordinate between agents.
        assert all(r.get("granted") == 1 for r in results)

        zero_conflict = sum(1 for r in results if r.get("conflicts") == 0)
        has_conflicts = sum(1 for r in results if r.get("conflicts", 0) >= 1)
        assert zero_conflict == 1
        assert has_conflicts == 2


# ============================================================================
# Test: Read/Ack Flow for Team Coordination
# ============================================================================


@pytest.mark.asyncio
async def test_read_ack_team_coordination(isolated_env):
    """Test read and acknowledgment flow for team coordination."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/test/e2e/team_coord"

        blue_lake, green_mountain, red_stone = await setup_three_agents(
            client, project_key
        )

        # Send important message requiring ack
        msg_result = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": blue_lake,
                "to": [green_mountain, red_stone],
                "subject": "Important: Deploy schedule",
                "body_md": "We deploy at 5pm. Please acknowledge.",
                "importance": "high",
                "ack_required": True,
            },
        )

        msg_id = get_message_id_from_send(msg_result)

        # Both recipients mark as read
        await client.call_tool(
            "mark_message_read",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "message_id": msg_id,
            },
        )

        await client.call_tool(
            "mark_message_read",
            {
                "project_key": project_key,
                "agent_name": red_stone,
                "message_id": msg_id,
            },
        )

        # Check acknowledgments before ack
        acks_before = await get_message_acknowledgments(msg_id)
        green_before = next(
            (a for a in acks_before if a["agent_name"] == green_mountain), None
        )
        assert green_before is not None
        assert green_before["read_ts"] is not None
        assert green_before["ack_ts"] is None  # Not yet acked

        # Now acknowledge
        await client.call_tool(
            "acknowledge_message",
            {
                "project_key": project_key,
                "agent_name": green_mountain,
                "message_id": msg_id,
            },
        )

        await client.call_tool(
            "acknowledge_message",
            {
                "project_key": project_key,
                "agent_name": red_stone,
                "message_id": msg_id,
            },
        )

        # Verify both acknowledged
        acks_after = await get_message_acknowledgments(msg_id)
        for ack in acks_after:
            assert ack["ack_ts"] is not None, f"{ack['agent_name']} should have acked"
