"""P0 Regression Tests: Datetime Naive/Aware Handling.

Background: We fixed a bug where SQLite (which stores naive datetimes) was compared
against timezone-aware Python datetimes, causing:
    TypeError: can't compare offset-naive and offset-aware datetimes

These tests prevent recurrence of this bug.

Test Cases:
1. _naive_utc() returns naive datetime when given None
2. _naive_utc() strips timezone from aware datetime
3. _utcnow_naive() model factory returns naive datetime
4. All model default_factory fields produce naive datetimes
5. File reservation expiration comparison works (original failure point)
6. AgentLink timestamp comparisons work
7. MessageRecipient timestamp updates work

Reference: mcp_agent_mail-yhk
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client

from mcp_agent_mail.app import _naive_utc, build_mcp_server
from mcp_agent_mail.models import (
    Agent,
    AgentLink,
    FileReservation,
    Message,
    Product,
    ProductProjectLink,
    Project,
    ProjectSiblingSuggestion,
    _utcnow_naive,
)

# ============================================================================
# Unit Tests: _naive_utc() helper in app.py
# ============================================================================


class TestNaiveUtcHelper:
    """Test the _naive_utc() helper function from app.py."""

    def test_naive_utc_returns_naive_when_given_none(self):
        """_naive_utc() should return current time as naive datetime when dt=None."""
        result = _naive_utc(None)
        assert result is not None
        assert result.tzinfo is None, "Result should be naive (no timezone)"
        # Should be close to now
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        assert abs((result - now_naive).total_seconds()) < 2

    def test_naive_utc_returns_naive_when_given_no_args(self):
        """_naive_utc() with no args should return naive datetime."""
        result = _naive_utc()
        assert result.tzinfo is None, "Result should be naive (no timezone)"

    def test_naive_utc_strips_timezone_from_aware_datetime(self):
        """_naive_utc() should strip timezone from aware datetime."""
        aware_dt = datetime(2025, 6, 15, 12, 30, 45, tzinfo=timezone.utc)
        result = _naive_utc(aware_dt)
        assert result.tzinfo is None, "Result should be naive"
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15
        assert result.hour == 12
        assert result.minute == 30
        assert result.second == 45

    def test_naive_utc_converts_non_utc_to_utc_then_strips(self):
        """_naive_utc() should convert non-UTC timezone to UTC first, then strip."""
        # Create a datetime in UTC+5
        from datetime import timezone as tz

        utc_plus_5 = tz(timedelta(hours=5))
        aware_dt = datetime(2025, 6, 15, 17, 30, 0, tzinfo=utc_plus_5)  # 17:30 UTC+5 = 12:30 UTC
        result = _naive_utc(aware_dt)
        assert result.tzinfo is None, "Result should be naive"
        assert result.hour == 12, "Should be converted to UTC (17:30 - 5h = 12:30)"
        assert result.minute == 30

    def test_naive_utc_passes_through_naive_datetime(self):
        """_naive_utc() should return naive datetime unchanged."""
        naive_dt = datetime(2025, 1, 1, 0, 0, 0)
        result = _naive_utc(naive_dt)
        assert result.tzinfo is None
        assert result == naive_dt


# ============================================================================
# Unit Tests: _utcnow_naive() model factory in models.py
# ============================================================================


class TestUtcnowNaiveFactory:
    """Test the _utcnow_naive() model factory function from models.py."""

    def test_utcnow_naive_returns_naive_datetime(self):
        """_utcnow_naive() should return naive datetime."""
        result = _utcnow_naive()
        assert result.tzinfo is None, "Result should be naive (no timezone)"

    def test_utcnow_naive_is_close_to_current_utc(self):
        """_utcnow_naive() should return time close to current UTC."""
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = _utcnow_naive()
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        assert before <= result <= after


# ============================================================================
# Unit Tests: Model default_factory fields produce naive datetimes
# ============================================================================


class TestModelDefaultsProduceNaiveDatetimes:
    """Verify all model default_factory fields produce naive datetimes."""

    def test_project_created_at_is_naive(self):
        """Project.created_at default should be naive."""
        project = Project(slug="test", human_key="/test")
        assert project.created_at.tzinfo is None, "Project.created_at should be naive"

    def test_product_created_at_is_naive(self):
        """Product.created_at default should be naive."""
        product = Product(product_uid="test-uid", name="Test Product")
        assert product.created_at.tzinfo is None, "Product.created_at should be naive"

    def test_product_project_link_created_at_is_naive(self):
        """ProductProjectLink.created_at default should be naive."""
        link = ProductProjectLink(product_id=1, project_id=1)
        assert link.created_at.tzinfo is None, "ProductProjectLink.created_at should be naive"

    def test_agent_timestamps_are_naive(self):
        """Agent inception_ts and last_active_ts defaults should be naive."""
        agent = Agent(project_id=1, name="TestAgent", program="test", model="test")
        assert agent.inception_ts.tzinfo is None, "Agent.inception_ts should be naive"
        assert agent.last_active_ts.tzinfo is None, "Agent.last_active_ts should be naive"

    def test_message_created_ts_is_naive(self):
        """Message.created_ts default should be naive."""
        msg = Message(project_id=1, sender_id=1, subject="Test", body_md="Body")
        assert msg.created_ts.tzinfo is None, "Message.created_ts should be naive"

    def test_file_reservation_created_ts_is_naive(self):
        """FileReservation.created_ts default should be naive."""
        # expires_ts is required, not default
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        reservation = FileReservation(
            project_id=1, agent_id=1, path_pattern="**", expires_ts=expires
        )
        assert reservation.created_ts.tzinfo is None, "FileReservation.created_ts should be naive"

    def test_agent_link_timestamps_are_naive(self):
        """AgentLink created_ts and updated_ts defaults should be naive."""
        link = AgentLink(
            a_project_id=1, a_agent_id=1, b_project_id=1, b_agent_id=2
        )
        assert link.created_ts.tzinfo is None, "AgentLink.created_ts should be naive"
        assert link.updated_ts.tzinfo is None, "AgentLink.updated_ts should be naive"

    def test_project_sibling_suggestion_timestamps_are_naive(self):
        """ProjectSiblingSuggestion created_ts and evaluated_ts defaults should be naive."""
        suggestion = ProjectSiblingSuggestion(project_a_id=1, project_b_id=2)
        assert suggestion.created_ts.tzinfo is None, "created_ts should be naive"
        assert suggestion.evaluated_ts.tzinfo is None, "evaluated_ts should be naive"


# ============================================================================
# Integration Tests: File Reservation Expiration Comparison
# (This was the original failure point)
# ============================================================================


@pytest.mark.asyncio
async def test_file_reservation_expiration_comparison_no_error(isolated_env):
    """File reservation expiration queries should not raise datetime comparison errors.

    This is the original failure point where comparing in-memory datetime objects
    with SQLite DATETIME columns failed due to naive/aware mismatch.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        # Setup: create project and agent (let server auto-generate name)
        await client.call_tool("ensure_project", {"human_key": "/test/regression"})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/regression",
                "program": "test",
                "model": "test",
            },
        )
        agent_name = agent_result.data["name"]

        # Create a file reservation
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/regression",
                "agent_name": agent_name,
                "paths": ["src/**"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert result.data["granted"], "Should grant reservation"

        # Now query file reservations - this is where the error occurred
        # The internal query compares expires_ts (naive in DB) with now (was aware)
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/regression",
                "agent_name": agent_name,
                "paths": ["other/**"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        # If we got here without TypeError, the fix is working
        assert "granted" in result.data or "conflicts" in result.data


@pytest.mark.asyncio
async def test_file_reservation_release_and_renew_datetime_handling(isolated_env):
    """Test that release and renew operations handle datetimes correctly."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/datetime"})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/datetime",
                "program": "test",
                "model": "test",
            },
        )
        agent_name = agent_result.data["name"]

        # Create reservation
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/test/datetime",
                "agent_name": agent_name,
                "paths": ["lib/**"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )

        # Renew reservation (involves datetime comparison)
        renew_result = await client.call_tool(
            "renew_file_reservations",
            {
                "project_key": "/test/datetime",
                "agent_name": agent_name,
                "extend_seconds": 600,
            },
        )
        assert renew_result.data["renewed"] >= 0

        # Release reservation (sets released_ts)
        release_result = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "/test/datetime",
                "agent_name": agent_name,
            },
        )
        assert release_result.data["released"] >= 0


# ============================================================================
# Integration Tests: AgentLink timestamp comparisons
# ============================================================================


@pytest.mark.asyncio
async def test_agent_link_expiration_comparison(isolated_env):
    """Test that agent link expiration handling works with naive datetimes."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/links"})
        agent_a_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/links",
                "program": "test",
                "model": "test",
            },
        )
        agent_a_name = agent_a_result.data["name"]

        agent_b_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/links",
                "program": "test",
                "model": "test",
            },
        )
        agent_b_name = agent_b_result.data["name"]

        # Request contact - creates AgentLink with timestamps
        result = await client.call_tool(
            "request_contact",
            {
                "project_key": "/test/links",
                "from_agent": agent_a_name,
                "to_agent": agent_b_name,
                "reason": "Testing datetime handling",
                "ttl_seconds": 3600,
            },
        )
        assert result.data["status"] == "pending"

        # Respond to contact - updates timestamps
        respond_result = await client.call_tool(
            "respond_contact",
            {
                "project_key": "/test/links",
                "to_agent": agent_b_name,
                "from_agent": agent_a_name,
                "accept": True,
            },
        )
        assert respond_result.data["approved"] is True


# ============================================================================
# Integration Tests: MessageRecipient timestamp updates
# ============================================================================


@pytest.mark.asyncio
async def test_message_recipient_timestamp_updates(isolated_env):
    """Test that message read/ack timestamps are set correctly as naive."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/msg"})
        sender_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/msg",
                "program": "test",
                "model": "test",
            },
        )
        sender_name = sender_result.data["name"]

        receiver_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/msg",
                "program": "test",
                "model": "test",
            },
        )
        receiver_name = receiver_result.data["name"]

        # Send message
        send_result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/msg",
                "sender_name": sender_name,
                "to": [receiver_name],
                "subject": "Datetime Test",
                "body_md": "Testing datetime handling",
                "ack_required": True,
            },
        )
        # send_message returns {"deliveries": [{"project": ..., "payload": {...}}], "count": N}
        message_id = send_result.data["deliveries"][0]["payload"]["id"]

        # Mark as read - sets read_ts
        read_result = await client.call_tool(
            "mark_message_read",
            {
                "project_key": "/test/msg",
                "agent_name": receiver_name,
                "message_id": message_id,
            },
        )
        assert read_result.data["read"] is True
        assert read_result.data["read_at"] is not None

        # Acknowledge - sets ack_ts
        ack_result = await client.call_tool(
            "acknowledge_message",
            {
                "project_key": "/test/msg",
                "agent_name": receiver_name,
                "message_id": message_id,
            },
        )
        assert ack_result.data["acknowledged"] is True
        assert ack_result.data["acknowledged_at"] is not None


# ============================================================================
# Regression Test: Inbox fetch with since_ts parameter
# ============================================================================


@pytest.mark.asyncio
async def test_inbox_fetch_with_since_ts_datetime_handling(isolated_env):
    """Test that fetch_inbox with since_ts handles datetime comparisons correctly."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/inbox"})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/inbox",
                "program": "test",
                "model": "test",
            },
        )
        agent_name = agent_result.data["name"]

        # Fetch inbox with since_ts (involves datetime comparison)
        result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/test/inbox",
                "agent_name": agent_name,
                "since_ts": "2025-01-01T00:00:00Z",
                "limit": 10,
            },
        )
        # Should return empty list, not raise TypeError
        assert isinstance(result.data, list)
