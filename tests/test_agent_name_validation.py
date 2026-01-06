"""P0 Regression Tests: Agent Name Validation.

These tests verify that agent name validation correctly:
1. Accepts valid adjective+noun combinations
2. Rejects invalid names (descriptive, program names, etc.)
3. Handles case insensitivity properly
4. Works correctly in registration and identity creation flows

Reference: mcp_agent_mail-2xf
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import (
    ADJECTIVES,
    NOUNS,
    generate_agent_name,
    sanitize_agent_name,
    validate_agent_name_format,
)

# ============================================================================
# Unit Tests: validate_agent_name_format()
# ============================================================================


class TestValidateAgentNameFormat:
    """Test the validate_agent_name_format() function from utils.py."""

    def test_valid_names_return_true(self):
        """Valid adjective+noun combinations should return True."""
        valid_names = [
            "GreenLake",
            "BlueDog",
            "RedStone",
            "PurpleBear",
            "WhiteMountain",
            "FrostyDog",
            "SilentCave",
            "BrightForest",
        ]
        for name in valid_names:
            assert validate_agent_name_format(name), f"'{name}' should be valid"

    def test_all_adjective_noun_combinations_are_valid(self):
        """Sample of all possible adjective+noun combinations should be valid."""
        # Test a representative sample (testing all 4278+ would be slow)
        sample_adjectives = list(ADJECTIVES)[:10]
        sample_nouns = list(NOUNS)[:10]
        for adj in sample_adjectives:
            for noun in sample_nouns:
                name = f"{adj}{noun}"
                assert validate_agent_name_format(name), f"'{name}' should be valid"

    def test_case_insensitive_validation(self):
        """Validation should be case-insensitive."""
        # All these variations should be valid
        assert validate_agent_name_format("GreenLake")
        assert validate_agent_name_format("greenlake")
        assert validate_agent_name_format("GREENLAKE")
        assert validate_agent_name_format("gReEnLaKe")
        assert validate_agent_name_format("greenLAKE")

    def test_invalid_names_return_false(self):
        """Invalid names should return False."""
        invalid_names = [
            "",  # Empty
            "   ",  # Whitespace only
            "Green",  # Adjective only
            "Lake",  # Noun only
            "BackendAgent",  # Descriptive
            "CodeMigrator",  # Descriptive
            "claude-code",  # Program name
            "gpt-4",  # Model name
            "user@example.com",  # Email
            "all",  # Broadcast keyword
            "LakeGreen",  # Reversed order
            "GreenGreen",  # Same word twice (adjective)
            "LakeLake",  # Same word twice (noun)
            "GreenLakeBlue",  # Three words
            "123Lake",  # Number prefix
            "Green123",  # Number suffix
            "Green_Lake",  # Underscore
            "Green-Lake",  # Hyphen
            "Green Lake",  # Space
        ]
        for name in invalid_names:
            assert not validate_agent_name_format(name), f"'{name}' should be invalid"

    def test_empty_string_returns_false(self):
        """Empty string should return False."""
        assert not validate_agent_name_format("")

    def test_none_like_empty_returns_false(self):
        """None-like inputs should return False."""
        # Note: The function expects a string, but we test edge cases
        assert not validate_agent_name_format("")
        assert not validate_agent_name_format("   ")

    def test_partial_matches_return_false(self):
        """Partial matches (adjective or noun only) should return False."""
        for adj in list(ADJECTIVES)[:5]:
            assert not validate_agent_name_format(adj), f"Adjective-only '{adj}' should be invalid"
        for noun in list(NOUNS)[:5]:
            assert not validate_agent_name_format(noun), f"Noun-only '{noun}' should be invalid"

    def test_reversed_order_returns_false(self):
        """Noun+adjective (wrong order) should return False."""
        reversed_names = ["LakeGreen", "DogBlue", "StoneRed", "BearPurple"]
        for name in reversed_names:
            assert not validate_agent_name_format(name), f"Reversed '{name}' should be invalid"


# ============================================================================
# Unit Tests: generate_agent_name()
# ============================================================================


class TestGenerateAgentName:
    """Test the generate_agent_name() function from utils.py."""

    def test_returns_string(self):
        """generate_agent_name() should return a string."""
        name = generate_agent_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_generated_names_are_valid(self):
        """All generated names should pass validation."""
        for _ in range(50):  # Generate 50 random names
            name = generate_agent_name()
            assert validate_agent_name_format(name), f"Generated name '{name}' should be valid"

    def test_generated_names_are_pascalcase(self):
        """Generated names should be in PascalCase format."""
        for _ in range(20):
            name = generate_agent_name()
            # Should start with uppercase
            assert name[0].isupper(), f"'{name}' should start with uppercase"
            # Should contain at least one more uppercase (start of noun)
            upper_count = sum(1 for c in name if c.isupper())
            assert upper_count >= 2, f"'{name}' should have at least 2 uppercase letters"

    def test_generated_names_use_word_lists(self):
        """Generated names should use words from ADJECTIVES and NOUNS lists."""
        adjectives_lower = {a.lower() for a in ADJECTIVES}
        nouns_lower = {n.lower() for n in NOUNS}

        for _ in range(30):
            name = generate_agent_name()
            name_lower = name.lower()
            # Check that name starts with an adjective and ends with a noun
            found_match = False
            for adj in adjectives_lower:
                if name_lower.startswith(adj):
                    remaining = name_lower[len(adj) :]
                    if remaining in nouns_lower:
                        found_match = True
                        break
            assert found_match, f"'{name}' should be composed of adjective+noun from word lists"


# ============================================================================
# Unit Tests: sanitize_agent_name()
# ============================================================================


class TestSanitizeAgentName:
    """Test the sanitize_agent_name() function from utils.py."""

    def test_strips_whitespace(self):
        """Whitespace should be stripped."""
        assert sanitize_agent_name("  GreenLake  ") == "GreenLake"
        assert sanitize_agent_name("\tBlueDog\n") == "BlueDog"

    def test_removes_special_characters(self):
        """Non-alphanumeric characters should be removed."""
        assert sanitize_agent_name("Green-Lake") == "GreenLake"
        assert sanitize_agent_name("Blue_Dog") == "BlueDog"
        assert sanitize_agent_name("Red.Stone") == "RedStone"
        assert sanitize_agent_name("Purple@Bear") == "PurpleBear"

    def test_preserves_alphanumeric(self):
        """Alphanumeric characters should be preserved."""
        assert sanitize_agent_name("GreenLake123") == "GreenLake123"
        assert sanitize_agent_name("Blue2Dog") == "Blue2Dog"

    def test_empty_after_cleanup_returns_none(self):
        """If nothing remains after cleanup, return None."""
        assert sanitize_agent_name("") is None
        assert sanitize_agent_name("   ") is None
        assert sanitize_agent_name("---") is None
        assert sanitize_agent_name("@#$%") is None

    def test_truncates_long_names(self):
        """Names longer than 128 characters should be truncated."""
        long_name = "A" * 200
        result = sanitize_agent_name(long_name)
        assert result is not None
        assert len(result) <= 128

    def test_preserves_case(self):
        """Case should be preserved."""
        assert sanitize_agent_name("greenLake") == "greenLake"
        assert sanitize_agent_name("GREENLAKE") == "GREENLAKE"
        assert sanitize_agent_name("GreenLake") == "GreenLake"


# ============================================================================
# Integration Tests: Agent Registration with Valid Names
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_auto_generates_valid_name(isolated_env):
    """register_agent should auto-generate a valid name when name is omitted."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/names"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/names",
                "program": "test-program",
                "model": "test-model",
            },
        )

        agent_name = result.data["name"]
        assert agent_name is not None
        assert validate_agent_name_format(agent_name), f"Auto-generated '{agent_name}' should be valid"


@pytest.mark.asyncio
async def test_register_agent_with_explicit_valid_name(isolated_env):
    """register_agent should accept explicit valid names."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/names"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/names",
                "program": "test-program",
                "model": "test-model",
                "name": "BlueMountain",
            },
        )

        assert result.data["name"] == "BlueMountain"


@pytest.mark.asyncio
async def test_register_agent_case_insensitive_uniqueness(isolated_env):
    """Agent names should be case-insensitively unique."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/case"})

        # Register with one case
        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/case",
                "program": "test",
                "model": "test",
                "name": "GreenLake",
            },
        )
        assert result1.data["name"] == "GreenLake"

        # Re-register with different case should update, not create new
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/case",
                "program": "test-updated",
                "model": "test-updated",
                "name": "greenlake",
            },
        )
        # Should return the same agent (same ID), with updated program
        assert result2.data["id"] == result1.data["id"]


# ============================================================================
# Integration Tests: Agent Registration with Invalid Names (Coerce Mode - Default)
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_coerces_invalid_descriptive_name(isolated_env):
    """In coerce mode (default), invalid names auto-generate valid ones."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/coerce"})

        # In coerce mode, invalid name should trigger auto-generation
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/coerce",
                "program": "test",
                "model": "test",
                "name": "BackendHarmonizer",  # Invalid descriptive name
            },
        )

        # Should get a valid auto-generated name, not the invalid one
        agent_name = result.data["name"]
        assert agent_name != "BackendHarmonizer", "Should not accept invalid name"
        assert validate_agent_name_format(agent_name), f"Auto-generated '{agent_name}' should be valid"


@pytest.mark.asyncio
async def test_register_agent_coerces_program_name_as_agent(isolated_env):
    """In coerce mode, program names as agent names get auto-generated."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/coerce"})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/coerce",
                "program": "claude-code",
                "model": "opus",
                "name": "claude-code",  # Using program name as agent name
            },
        )

        agent_name = result.data["name"]
        assert agent_name != "claude-code", "Should not accept program name as agent name"
        assert validate_agent_name_format(agent_name), f"Auto-generated '{agent_name}' should be valid"


# ============================================================================
# Integration Tests: Agent Registration with Invalid Names (Strict Mode)
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_strict_rejects_invalid_descriptive_name(isolated_env, monkeypatch):
    """In strict mode, register_agent should reject descriptive names."""
    monkeypatch.setenv("AGENT_NAME_ENFORCEMENT_MODE", "strict")
    # Clear cached settings to pick up the new env var
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/strict"})

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "/test/strict",
                    "program": "test",
                    "model": "test",
                    "name": "BackendHarmonizer",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "descriptive" in error_msg or "adjective" in error_msg or "invalid" in error_msg


@pytest.mark.asyncio
async def test_register_agent_strict_rejects_program_name_as_agent(isolated_env, monkeypatch):
    """In strict mode, register_agent should reject program names as agent names."""
    monkeypatch.setenv("AGENT_NAME_ENFORCEMENT_MODE", "strict")
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/strict"})

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "/test/strict",
                    "program": "claude-code",
                    "model": "opus",
                    "name": "claude-code",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "program" in error_msg or "adjective" in error_msg or "invalid" in error_msg


# ============================================================================
# Integration Tests: create_agent_identity
# ============================================================================


@pytest.mark.asyncio
async def test_create_agent_identity_generates_unique_names(isolated_env):
    """create_agent_identity should generate unique valid names."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/identity"})

        # Create multiple identities - all should have unique valid names
        names = set()
        for _ in range(5):
            result = await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": "/test/identity",
                    "program": "test",
                    "model": "test",
                },
            )
            name = result.data["name"]
            assert validate_agent_name_format(name), f"'{name}' should be valid"
            assert name not in names, f"'{name}' should be unique"
            names.add(name)


@pytest.mark.asyncio
async def test_create_agent_identity_with_valid_hint(isolated_env):
    """create_agent_identity should accept valid name hints."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/hint"})

        result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/hint",
                "program": "test",
                "model": "test",
                "name_hint": "SilentCave",
            },
        )

        assert result.data["name"] == "SilentCave"


@pytest.mark.asyncio
async def test_create_agent_identity_coerces_invalid_hint(isolated_env):
    """In coerce mode, create_agent_identity auto-generates for invalid hints."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/hint"})

        # In coerce mode, invalid hint should trigger auto-generation
        result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "/test/hint",
                "program": "test",
                "model": "test",
                "name_hint": "InvalidDescriptiveName",
            },
        )

        agent_name = result.data["name"]
        assert agent_name != "InvalidDescriptiveName", "Should not accept invalid hint"
        assert validate_agent_name_format(agent_name), f"Auto-generated '{agent_name}' should be valid"


@pytest.mark.asyncio
async def test_create_agent_identity_strict_rejects_invalid_hint(isolated_env, monkeypatch):
    """In strict mode, create_agent_identity should reject invalid name hints."""
    monkeypatch.setenv("AGENT_NAME_ENFORCEMENT_MODE", "strict")
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/strict-hint"})

        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": "/test/strict-hint",
                    "program": "test",
                    "model": "test",
                    "name_hint": "InvalidDescriptiveName",
                },
            )

        error_msg = str(exc_info.value).lower()
        assert "adjective" in error_msg or "invalid" in error_msg or "format" in error_msg


# ============================================================================
# Integration Tests: Message Sending with Agent Names
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_validates_recipient_names(isolated_env):
    """send_message should validate recipient agent names exist."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/msg"})

        # Register sender
        sender_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/msg",
                "program": "test",
                "model": "test",
            },
        )
        sender_name = sender_result.data["name"]

        # Try to send to non-existent recipient - use a valid-format name that doesn't exist
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/test/msg",
                    "sender_name": sender_name,
                    "to": ["SilentGlacier"],  # Valid format but doesn't exist
                    "subject": "Test",
                    "body_md": "Test message",
                },
            )

        error_msg = str(exc_info.value).lower()
        # Error should indicate the agent was not found
        assert "not found" in error_msg or "not registered" in error_msg or "available" in error_msg


@pytest.mark.asyncio
async def test_send_message_with_valid_agents(isolated_env):
    """send_message should work with valid, registered agent names."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/test/valid"})

        # Register sender and recipient
        sender_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/valid",
                "program": "test",
                "model": "test",
            },
        )
        sender_name = sender_result.data["name"]

        recipient_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/test/valid",
                "program": "test",
                "model": "test",
            },
        )
        recipient_name = recipient_result.data["name"]

        # Send message should succeed
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/test/valid",
                "sender_name": sender_name,
                "to": [recipient_name],
                "subject": "Test Message",
                "body_md": "Testing valid agent names",
            },
        )

        assert result.data["count"] == 1
        assert len(result.data["deliveries"]) == 1


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestAgentNameEdgeCases:
    """Test edge cases in agent name handling."""

    def test_adjectives_are_non_empty(self):
        """ADJECTIVES list should be non-empty."""
        assert len(list(ADJECTIVES)) > 0

    def test_nouns_are_non_empty(self):
        """NOUNS list should be non-empty."""
        assert len(list(NOUNS)) > 0

    def test_all_adjectives_are_capitalized(self):
        """All adjectives should be capitalized."""
        for adj in ADJECTIVES:
            assert adj[0].isupper(), f"Adjective '{adj}' should start with uppercase"

    def test_all_nouns_are_capitalized(self):
        """All nouns should be capitalized."""
        for noun in NOUNS:
            assert noun[0].isupper(), f"Noun '{noun}' should start with uppercase"

    def test_no_duplicate_adjectives(self):
        """ADJECTIVES should have no duplicates (case-insensitive)."""
        adj_lower = [a.lower() for a in ADJECTIVES]
        assert len(adj_lower) == len(set(adj_lower)), "Duplicate adjectives found"

    def test_no_duplicate_nouns(self):
        """NOUNS should have no duplicates (case-insensitive)."""
        nouns_lower = [n.lower() for n in NOUNS]
        assert len(nouns_lower) == len(set(nouns_lower)), "Duplicate nouns found"

    def test_namespace_size(self):
        """Verify the namespace is large enough for practical use."""
        num_adjectives = len(list(ADJECTIVES))
        num_nouns = len(list(NOUNS))
        namespace_size = num_adjectives * num_nouns
        # Should have at least 4000 combinations (62 x 69 = 4278 per the comment)
        assert namespace_size >= 4000, f"Namespace too small: {namespace_size}"
