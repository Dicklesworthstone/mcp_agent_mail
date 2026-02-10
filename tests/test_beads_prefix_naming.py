"""Tests for Beads-based agent name generation.

These tests verify that agent names can be generated based on Beads issue prefixes,
providing project-specific naming that helps identify which project an agent belongs to.

Examples:
- td-core -> TC -> TealCanyon, TurquoiseCastle, TopazCave
- beads -> BE -> BronzeElk, BlueEagle, BrightElk
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import (
    ADJECTIVES_BY_LETTER,
    NOUNS_BY_LETTER,
    generate_agent_name,
    get_beads_issue_prefix,
    parse_beads_prefix_to_initials,
    validate_agent_name_format,
)


# ============================================================================
# Unit Tests: parse_beads_prefix_to_initials()
# ============================================================================


class TestParseBeadsPrefixToInitials:
    """Test the parse_beads_prefix_to_initials() function."""

    def test_hyphenated_prefix_two_segments(self):
        """Two-segment prefix returns first letters of each segment."""
        assert parse_beads_prefix_to_initials("td-core") == "TC"
        assert parse_beads_prefix_to_initials("my-app") == "MA"

    def test_hyphenated_prefix_three_segments(self):
        """Three+ segment prefix returns first letters of first two segments."""
        assert parse_beads_prefix_to_initials("my-app-name") == "MA"
        assert parse_beads_prefix_to_initials("a-b-c-d") == "AB"

    def test_single_segment_prefix(self):
        """Single segment prefix returns first two letters."""
        assert parse_beads_prefix_to_initials("beads") == "BE"
        assert parse_beads_prefix_to_initials("project") == "PR"
        assert parse_beads_prefix_to_initials("xy") == "XY"

    def test_uppercase_output(self):
        """Initials are always uppercase."""
        assert parse_beads_prefix_to_initials("td-core") == "TC"
        assert parse_beads_prefix_to_initials("TD-CORE") == "TC"
        assert parse_beads_prefix_to_initials("Td-Core") == "TC"

    def test_empty_prefix_returns_none(self):
        """Empty or whitespace-only prefix returns None."""
        assert parse_beads_prefix_to_initials("") is None
        assert parse_beads_prefix_to_initials("   ") is None

    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_beads_prefix_to_initials(None) is None

    def test_single_character_returns_none(self):
        """Single character prefix returns None (need 2 letters)."""
        assert parse_beads_prefix_to_initials("a") is None

    def test_empty_segments_returns_none(self):
        """Empty segments in hyphenated prefix return None."""
        assert parse_beads_prefix_to_initials("-core") is None
        assert parse_beads_prefix_to_initials("td-") is None
        assert parse_beads_prefix_to_initials("-") is None

    def test_strips_whitespace(self):
        """Leading/trailing whitespace is stripped."""
        assert parse_beads_prefix_to_initials("  td-core  ") == "TC"
        assert parse_beads_prefix_to_initials("\tbeads\n") == "BE"


# ============================================================================
# Unit Tests: get_beads_issue_prefix()
# ============================================================================


class TestGetBeadsIssuePrefix:
    """Test the get_beads_issue_prefix() function."""

    def test_returns_prefix_from_beads_db(self):
        """Returns issue_prefix from .beads/beads.db config table."""
        with tempfile.TemporaryDirectory() as tmpdir:
            beads_dir = Path(tmpdir) / ".beads"
            beads_dir.mkdir()
            db_path = beads_dir / "beads.db"

            # Create a minimal beads.db with config table
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO config VALUES ('issue_prefix', 'td-core')")
            conn.commit()
            conn.close()

            result = get_beads_issue_prefix(tmpdir)
            assert result == "td-core"

    def test_returns_none_when_no_beads_dir(self):
        """Returns None when .beads directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = get_beads_issue_prefix(tmpdir)
            assert result is None

    def test_returns_none_when_no_beads_db(self):
        """Returns None when beads.db doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            beads_dir = Path(tmpdir) / ".beads"
            beads_dir.mkdir()

            result = get_beads_issue_prefix(tmpdir)
            assert result is None

    def test_returns_none_when_no_config_table(self):
        """Returns None when config table doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            beads_dir = Path(tmpdir) / ".beads"
            beads_dir.mkdir()
            db_path = beads_dir / "beads.db"

            conn = sqlite3.connect(str(db_path))
            conn.close()

            result = get_beads_issue_prefix(tmpdir)
            assert result is None

    def test_returns_none_when_no_issue_prefix(self):
        """Returns None when issue_prefix key doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            beads_dir = Path(tmpdir) / ".beads"
            beads_dir.mkdir()
            db_path = beads_dir / "beads.db"

            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO config VALUES ('other_key', 'some_value')")
            conn.commit()
            conn.close()

            result = get_beads_issue_prefix(tmpdir)
            assert result is None

    def test_returns_none_when_issue_prefix_empty(self):
        """Returns None when issue_prefix value is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            beads_dir = Path(tmpdir) / ".beads"
            beads_dir.mkdir()
            db_path = beads_dir / "beads.db"

            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO config VALUES ('issue_prefix', '')")
            conn.commit()
            conn.close()

            result = get_beads_issue_prefix(tmpdir)
            assert result is None

    def test_handles_invalid_path_gracefully(self):
        """Returns None for invalid paths without raising."""
        result = get_beads_issue_prefix("/nonexistent/path/to/project")
        assert result is None


# ============================================================================
# Unit Tests: generate_agent_name() with initials
# ============================================================================


class TestGenerateAgentNameWithInitials:
    """Test the generate_agent_name() function with initials parameter."""

    def test_generates_name_with_valid_initials(self):
        """Generates name matching the provided initials."""
        # TC should give us T* adjective + C* noun
        name = generate_agent_name("TC")
        assert name[0] == "T", f"Expected name starting with T, got {name}"
        assert validate_agent_name_format(name), f"Generated name '{name}' should be valid"

        # Find where the noun starts (first capital after position 0)
        noun_start = -1
        for i in range(1, len(name)):
            if name[i].isupper():
                noun_start = i
                break
        assert noun_start > 0, f"Could not find noun start in {name}"
        assert name[noun_start] == "C", f"Expected noun starting with C in {name}"

    def test_generates_different_names_with_same_initials(self):
        """Multiple calls with same initials generate variety."""
        names = {generate_agent_name("TC") for _ in range(50)}
        # Should get at least a few different names
        assert len(names) >= 2, f"Expected variety, got only: {names}"

    def test_all_generated_names_are_valid(self):
        """All generated names pass validation."""
        initials_to_test = ["TC", "BE", "MA", "GR", "BL"]
        for initials in initials_to_test:
            for _ in range(10):
                name = generate_agent_name(initials)
                assert validate_agent_name_format(name), f"'{name}' with initials '{initials}' should be valid"

    def test_falls_back_for_missing_adjectives(self):
        """Falls back to random when no adjectives for first letter."""
        # X is not a starting letter for any adjective in our list
        name = generate_agent_name("XY")
        assert validate_agent_name_format(name), f"Fallback name '{name}' should be valid"

    def test_falls_back_for_missing_nouns(self):
        """Falls back to random when no nouns for second letter."""
        # B has adjectives, but X is not a starting letter for nouns
        name = generate_agent_name("BX")
        assert validate_agent_name_format(name), f"Fallback name '{name}' should be valid"

    def test_falls_back_for_invalid_initials(self):
        """Falls back to random for invalid initials."""
        # Single character
        name = generate_agent_name("T")
        assert validate_agent_name_format(name)

        # Three characters
        name = generate_agent_name("TCX")
        assert validate_agent_name_format(name)

        # Non-alpha
        name = generate_agent_name("12")
        assert validate_agent_name_format(name)

        # Empty
        name = generate_agent_name("")
        assert validate_agent_name_format(name)

    def test_none_initials_generates_random(self):
        """None initials generates fully random name."""
        name = generate_agent_name(None)
        assert validate_agent_name_format(name)

    def test_lowercase_initials_work(self):
        """Lowercase initials are converted to uppercase."""
        name = generate_agent_name("tc")
        assert name[0] == "T", f"Expected name starting with T, got {name}"


# ============================================================================
# Unit Tests: ADJECTIVES_BY_LETTER and NOUNS_BY_LETTER
# ============================================================================


class TestWordsByLetter:
    """Test the ADJECTIVES_BY_LETTER and NOUNS_BY_LETTER dictionaries."""

    def test_adjectives_by_letter_has_entries(self):
        """ADJECTIVES_BY_LETTER is populated."""
        assert len(ADJECTIVES_BY_LETTER) > 0
        # Should have multiple letters represented
        assert len(ADJECTIVES_BY_LETTER) >= 10

    def test_nouns_by_letter_has_entries(self):
        """NOUNS_BY_LETTER is populated."""
        assert len(NOUNS_BY_LETTER) > 0
        # Should have multiple letters represented
        assert len(NOUNS_BY_LETTER) >= 10

    def test_adjectives_grouped_correctly(self):
        """Each adjective is under its first letter."""
        for letter, words in ADJECTIVES_BY_LETTER.items():
            for word in words:
                assert word[0].upper() == letter, f"'{word}' should be under '{word[0].upper()}', not '{letter}'"

    def test_nouns_grouped_correctly(self):
        """Each noun is under its first letter."""
        for letter, words in NOUNS_BY_LETTER.items():
            for word in words:
                assert word[0].upper() == letter, f"'{word}' should be under '{word[0].upper()}', not '{letter}'"

    def test_common_letters_have_adjectives(self):
        """Common letters have adjective entries."""
        common_letters = ["B", "C", "G", "S", "R", "T"]
        for letter in common_letters:
            assert letter in ADJECTIVES_BY_LETTER, f"Expected adjectives starting with '{letter}'"
            assert len(ADJECTIVES_BY_LETTER[letter]) > 0

    def test_common_letters_have_nouns(self):
        """Common letters have noun entries."""
        common_letters = ["B", "C", "D", "F", "G", "H", "M", "R", "S"]
        for letter in common_letters:
            assert letter in NOUNS_BY_LETTER, f"Expected nouns starting with '{letter}'"
            assert len(NOUNS_BY_LETTER[letter]) > 0


# ============================================================================
# Integration Tests: Agent Registration with Beads
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_uses_beads_prefix(isolated_env, tmp_path, monkeypatch):
    """register_agent uses Beads prefix for name generation when available."""
    # Create a project directory with Beads config
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()
    beads_dir = project_dir / ".beads"
    beads_dir.mkdir()
    db_path = beads_dir / "beads.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('issue_prefix', 'td-core')")
    conn.commit()
    conn.close()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": str(project_dir)})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": str(project_dir),
                "program": "test-program",
                "model": "test-model",
            },
        )

        agent_name = result.data["name"]
        assert validate_agent_name_format(agent_name), f"'{agent_name}' should be valid"

        # Name should start with T (from "td-core" -> TC)
        assert agent_name[0] == "T", f"Expected name starting with T for td-core prefix, got {agent_name}"

        # Find noun start and verify it starts with C
        noun_start = -1
        for i in range(1, len(agent_name)):
            if agent_name[i].isupper():
                noun_start = i
                break
        assert noun_start > 0, f"Could not find noun start in {agent_name}"
        assert agent_name[noun_start] == "C", f"Expected noun starting with C in {agent_name}"


@pytest.mark.asyncio
async def test_register_agent_falls_back_without_beads(isolated_env, tmp_path):
    """register_agent generates random name when no Beads config exists."""
    # Create a project directory WITHOUT Beads config
    project_dir = tmp_path / "no-beads-project"
    project_dir.mkdir()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": str(project_dir)})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": str(project_dir),
                "program": "test-program",
                "model": "test-model",
            },
        )

        agent_name = result.data["name"]
        assert validate_agent_name_format(agent_name), f"'{agent_name}' should be valid"


@pytest.mark.asyncio
async def test_multiple_agents_same_project_get_unique_names(isolated_env, tmp_path):
    """Multiple agents in same project get unique names with same initials."""
    project_dir = tmp_path / "multi-agent-project"
    project_dir.mkdir()
    beads_dir = project_dir / ".beads"
    beads_dir.mkdir()
    db_path = beads_dir / "beads.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('issue_prefix', 'td-core')")
    conn.commit()
    conn.close()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": str(project_dir)})

        names = set()
        for _ in range(5):
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": str(project_dir),
                    "program": "test-program",
                    "model": "test-model",
                },
            )
            name = result.data["name"]
            assert name not in names, f"Got duplicate name: {name}"
            names.add(name)

        # All names should be unique
        assert len(names) == 5


@pytest.mark.asyncio
async def test_explicit_name_overrides_beads_prefix(isolated_env, tmp_path):
    """Explicit name parameter overrides Beads-based generation."""
    project_dir = tmp_path / "explicit-name-project"
    project_dir.mkdir()
    beads_dir = project_dir / ".beads"
    beads_dir.mkdir()
    db_path = beads_dir / "beads.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('issue_prefix', 'td-core')")
    conn.commit()
    conn.close()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": str(project_dir)})

        # Register with explicit name (not matching TC initials)
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": str(project_dir),
                "program": "test-program",
                "model": "test-model",
                "name": "BlueMountain",  # Not a TC name
            },
        )

        assert result.data["name"] == "BlueMountain"
