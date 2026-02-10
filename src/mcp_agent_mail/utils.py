"""Utility helpers for the MCP Agent Mail service."""

from __future__ import annotations

import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

# Agent name word lists - used to generate memorable adjective+noun combinations
# These lists are designed to provide a large namespace (62 x 69 = 4278 combinations)
# while keeping names easy to remember, spell, and distinguish.
#
# Design principles:
# - All words are capitalized for consistent CamelCase output (e.g., "GreenLake")
# - Adjectives are colors, weather, materials, and nature-themed descriptors
# - Nouns are nature, geography, animals, and simple objects
# - No offensive, controversial, or confusing words
# - No words that could be easily misspelled or confused with each other

ADJECTIVES: Iterable[str] = (
    # Colors (original + expanded)
    "Red",
    "Orange",
    "Pink",
    "Black",
    "Purple",
    "Blue",
    "Brown",
    "White",
    "Green",
    "Chartreuse",
    "Lilac",
    "Fuchsia",
    "Azure",
    "Amber",
    "Coral",
    "Crimson",
    "Cyan",
    "Gold",
    "Gray",
    "Indigo",
    "Ivory",
    "Jade",
    "Lavender",
    "Magenta",
    "Maroon",
    "Navy",
    "Olive",
    "Pearl",
    "Rose",
    "Ruby",
    "Sage",
    "Scarlet",
    "Silver",
    "Teal",
    "Topaz",
    "Violet",
    "Cobalt",
    "Copper",
    "Bronze",
    "Emerald",
    "Sapphire",
    "Turquoise",
    # Weather and nature
    "Sunny",
    "Misty",
    "Foggy",
    "Stormy",
    "Windy",
    "Frosty",
    "Dusty",
    "Hazy",
    "Cloudy",
    "Rainy",
    # Descriptive
    "Swift",
    "Quiet",
    "Bold",
    "Calm",
    "Bright",
    "Dark",
    "Wild",
    "Silent",
    "Gentle",
    "Rustic",
)

NOUNS: Iterable[str] = (
    # Original nouns
    "Stone",
    "Lake",
    "Dog",
    "Creek",
    "Pond",
    "Cat",
    "Bear",
    "Mountain",
    "Hill",
    "Snow",
    "Castle",
    # Geography and nature
    "River",
    "Forest",
    "Valley",
    "Canyon",
    "Meadow",
    "Prairie",
    "Desert",
    "Island",
    "Cliff",
    "Cave",
    "Glacier",
    "Waterfall",
    "Spring",
    "Stream",
    "Reef",
    "Dune",
    "Ridge",
    "Peak",
    "Gorge",
    "Marsh",
    "Brook",
    "Glen",
    "Grove",
    "Hollow",
    "Basin",
    "Cove",
    "Bay",
    "Harbor",
    # Animals
    "Fox",
    "Wolf",
    "Hawk",
    "Eagle",
    "Owl",
    "Deer",
    "Elk",
    "Moose",
    "Falcon",
    "Raven",
    "Heron",
    "Crane",
    "Otter",
    "Beaver",
    "Badger",
    "Finch",
    "Robin",
    "Sparrow",
    "Lynx",
    "Puma",
    # Objects and structures
    "Tower",
    "Bridge",
    "Forge",
    "Mill",
    "Barn",
    "Gate",
    "Anchor",
    "Lantern",
    "Beacon",
    "Compass",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_AGENT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _build_words_by_letter(words: Iterable[str]) -> dict[str, list[str]]:
    """Organize words into a dictionary keyed by uppercase first letter."""
    by_letter: dict[str, list[str]] = defaultdict(list)
    for word in words:
        if word:
            by_letter[word[0].upper()].append(word)
    return dict(by_letter)


# Organize word lists by first letter for efficient initial-based lookup.
ADJECTIVES_BY_LETTER: dict[str, list[str]] = _build_words_by_letter(ADJECTIVES)
NOUNS_BY_LETTER: dict[str, list[str]] = _build_words_by_letter(NOUNS)


# Pre-built frozenset of all valid agent names (lowercase) for O(1) validation lookup.
# This is computed once at module load time rather than O(n*m) per validation call.
_VALID_AGENT_NAMES: frozenset[str] = frozenset(
    f"{adj}{noun}".lower() for adj in ADJECTIVES for noun in NOUNS
)


def slugify(value: str) -> str:
    """Normalize a human-readable value into a slug."""
    normalized = value.strip().lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "project"


def parse_beads_prefix_to_initials(prefix: str) -> Optional[str]:
    """Parse a Beads issue prefix into two uppercase initials.

    Examples:
        "td-core" -> "TC"
        "beads" -> "BE"
        "my-app-name" -> "MA" (first two segments only)
        "" -> None
        None -> None

    Returns None if the prefix is empty or cannot be parsed.
    """
    if not prefix:
        return None

    prefix = prefix.strip()
    if not prefix:
        return None

    segments = prefix.split("-")
    if len(segments) >= 2:
        # Take first letter of first two segments
        first = segments[0][0] if segments[0] else ""
        second = segments[1][0] if segments[1] else ""
    else:
        # Single segment: take first two letters
        first = prefix[0] if len(prefix) >= 1 else ""
        second = prefix[1] if len(prefix) >= 2 else ""

    if not first or not second:
        return None

    return (first + second).upper()


def get_beads_issue_prefix(project_path: str) -> Optional[str]:
    """Read the issue_prefix from a project's Beads configuration.

    Looks for .beads/beads.db in the project directory and queries
    the config table for the issue_prefix value.

    Returns None if Beads is not configured or the prefix is not set.
    """
    beads_db = Path(project_path) / ".beads" / "beads.db"
    if not beads_db.exists():
        return None

    try:
        conn = sqlite3.connect(str(beads_db))
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'issue_prefix'")
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except (sqlite3.Error, OSError):
        pass

    return None


def generate_agent_name(initials: Optional[str] = None) -> str:
    """Return a random adjective+noun combination.

    If initials are provided (e.g., "TC"), generates a name where:
    - The adjective starts with the first letter (e.g., "T" -> "Teal")
    - The noun starts with the second letter (e.g., "C" -> "Canyon")

    Falls back to fully random generation if:
    - No initials provided
    - Initials are invalid (not exactly 2 letters)
    - No words available for the given initials
    """
    if initials and len(initials) == 2 and initials.isalpha():
        first_letter = initials[0].upper()
        second_letter = initials[1].upper()

        adjectives = ADJECTIVES_BY_LETTER.get(first_letter, [])
        nouns = NOUNS_BY_LETTER.get(second_letter, [])

        if adjectives and nouns:
            adjective = random.choice(adjectives)
            noun = random.choice(nouns)
            return f"{adjective}{noun}"

    # Fallback to random generation
    adjective = random.choice(tuple(ADJECTIVES))
    noun = random.choice(tuple(NOUNS))
    return f"{adjective}{noun}"


def validate_agent_name_format(name: str) -> bool:
    """
    Validate that an agent name matches the required adjective+noun format.

    CRITICAL: Agent names MUST be randomly generated two-word combinations
    like "GreenLake" or "BlueDog", NOT descriptive names like "BackendHarmonizer".

    Names should be:
    - Unique and easy to remember
    - NOT descriptive of the agent's role or task
    - One of the predefined adjective+noun combinations

    Note: This validation is case-insensitive to match the database behavior
    where "GreenLake", "greenlake", and "GREENLAKE" are treated as the same.

    Returns True if valid, False otherwise.
    """
    if not name:
        return False

    # O(1) lookup using pre-built frozenset (vs O(n*m) iteration)
    return name.lower() in _VALID_AGENT_NAMES


def sanitize_agent_name(value: str) -> Optional[str]:
    """Normalize user-provided agent name; return None if nothing remains."""
    cleaned = _AGENT_NAME_RE.sub("", value.strip())
    if not cleaned:
        return None
    return cleaned[:128]


def validate_thread_id_format(thread_id: str) -> bool:
    """Validate that a thread_id is safe for filenames and indexing.

    Thread IDs are used as human-facing keys and may also be used in filesystem
    paths for thread digests. For safety and portability, enforce:
    - ASCII alphanumerics plus '.', '_', '-'
    - Must start with an alphanumeric character
    - Max length 128
    """
    candidate = (thread_id or "").strip()
    if not candidate:
        return False
    return _THREAD_ID_RE.fullmatch(candidate) is not None
