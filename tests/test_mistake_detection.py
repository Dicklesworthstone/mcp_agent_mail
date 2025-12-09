"""Tests for common mistake detection helpers and UX improvements.

These tests verify that the mistake detection helpers correctly identify:
1. Program names mistakenly used as agent names
2. Model names mistakenly used as agent names
3. Email addresses mistakenly used as agent names
4. Broadcast attempts
5. Descriptive role names
6. Suspicious file reservation patterns
"""
from __future__ import annotations

import pytest

from mcp_agent_mail.app import (
    _detect_agent_name_mistake,
    _detect_suspicious_file_reservation,
    _looks_like_broadcast,
    _looks_like_descriptive_name,
    _looks_like_email,
    _looks_like_model_name,
    _looks_like_program_name,
)


class TestLooksLikeProgramName:
    """Tests for _looks_like_program_name helper."""

    def test_known_programs_are_detected(self):
        """Known program names should be detected."""
        programs = [
            "claude-code", "claude", "codex-cli", "codex", "cursor",
            "windsurf", "cline", "aider", "copilot", "github-copilot",
        ]
        for prog in programs:
            assert _looks_like_program_name(prog), f"Should detect '{prog}' as program name"

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        assert _looks_like_program_name("CLAUDE-CODE")
        assert _looks_like_program_name("Claude")
        assert _looks_like_program_name("CODEX")

    def test_valid_agent_names_not_detected(self):
        """Valid agent names should not be detected as program names."""
        valid_names = ["GreenLake", "BlueDog", "RedStone", "PurpleBear"]
        for name in valid_names:
            assert not _looks_like_program_name(name), f"'{name}' should not be detected as program"


class TestLooksLikeModelName:
    """Tests for _looks_like_model_name helper."""

    def test_known_model_patterns_detected(self):
        """Known model name patterns should be detected."""
        models = [
            "gpt-4", "gpt4-turbo", "claude-opus", "opus-4.5",
            "sonnet-3.5", "haiku", "gemini-pro", "llama-3",
            "o1-preview", "o3-mini",
        ]
        for model in models:
            assert _looks_like_model_name(model), f"Should detect '{model}' as model name"

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        assert _looks_like_model_name("GPT-4")
        assert _looks_like_model_name("Claude-Opus")
        assert _looks_like_model_name("SONNET")

    def test_valid_agent_names_not_detected(self):
        """Valid agent names should not be detected as model names."""
        valid_names = ["GreenLake", "BlueDog", "RedStone"]
        for name in valid_names:
            assert not _looks_like_model_name(name), f"'{name}' should not be detected as model"


class TestLooksLikeEmail:
    """Tests for _looks_like_email helper."""

    def test_valid_emails_detected(self):
        """Email addresses should be detected."""
        emails = [
            "user@example.com",
            "agent@company.org",
            "test.user@domain.co.uk",
        ]
        for email in emails:
            assert _looks_like_email(email), f"Should detect '{email}' as email"

    def test_agent_names_not_detected(self):
        """Agent names should not be detected as emails."""
        assert not _looks_like_email("GreenLake")
        assert not _looks_like_email("BlueDog")

    def test_at_sign_only_not_detected(self):
        """Just an @ sign without proper domain should not be detected."""
        assert not _looks_like_email("user@")
        assert not _looks_like_email("@domain")


class TestLooksLikeBroadcast:
    """Tests for _looks_like_broadcast helper."""

    def test_broadcast_keywords_detected(self):
        """Broadcast keywords should be detected."""
        broadcasts = ["all", "*", "everyone", "broadcast", "@all", "@everyone"]
        for b in broadcasts:
            assert _looks_like_broadcast(b), f"Should detect '{b}' as broadcast"

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        assert _looks_like_broadcast("ALL")
        assert _looks_like_broadcast("Everyone")
        assert _looks_like_broadcast("BROADCAST")

    def test_agent_names_not_detected(self):
        """Agent names should not be detected as broadcasts."""
        assert not _looks_like_broadcast("GreenLake")
        assert not _looks_like_broadcast("BlueDog")


class TestLooksLikeDescriptiveName:
    """Tests for _looks_like_descriptive_name helper."""

    def test_descriptive_suffixes_detected(self):
        """Names ending in descriptive suffixes should be detected."""
        descriptive_names = [
            "BackendAgent", "CodeBot", "TaskAssistant", "DatabaseHelper",
            "ProjectManager", "TeamCoordinator", "SoftwareDeveloper",
            "SystemEngineer", "CodeMigrator", "LegacyRefactorer",
            "BugFixer", "APIHarmonizer", "SystemIntegrator",
            "QueryOptimizer", "CodeAnalyzer", "BackgroundWorker",
        ]
        for name in descriptive_names:
            assert _looks_like_descriptive_name(name), f"Should detect '{name}' as descriptive"

    def test_valid_agent_names_not_detected(self):
        """Valid adjective+noun names should not be detected as descriptive."""
        valid_names = ["GreenLake", "BlueDog", "RedStone", "PurpleBear", "WhiteMountain"]
        for name in valid_names:
            assert not _looks_like_descriptive_name(name), f"'{name}' should not be detected"


class TestDetectAgentNameMistake:
    """Tests for _detect_agent_name_mistake comprehensive detector."""

    def test_detects_program_name(self):
        """Should detect program names with helpful message."""
        result = _detect_agent_name_mistake("claude-code")
        assert result is not None
        assert result[0] == "PROGRAM_NAME_AS_AGENT"
        assert "program name" in result[1].lower()

    def test_detects_model_name(self):
        """Should detect model names with helpful message."""
        result = _detect_agent_name_mistake("gpt-4-turbo")
        assert result is not None
        assert result[0] == "MODEL_NAME_AS_AGENT"
        assert "model name" in result[1].lower()

    def test_detects_email(self):
        """Should detect email addresses with helpful message."""
        result = _detect_agent_name_mistake("agent@example.com")
        assert result is not None
        assert result[0] == "EMAIL_AS_AGENT"
        assert "email" in result[1].lower()

    def test_detects_broadcast(self):
        """Should detect broadcast attempts with helpful message."""
        result = _detect_agent_name_mistake("all")
        assert result is not None
        assert result[0] == "BROADCAST_ATTEMPT"
        assert "broadcast" in result[1].lower()

    def test_detects_descriptive_name(self):
        """Should detect descriptive role names with helpful message."""
        result = _detect_agent_name_mistake("BackendHarmonizer")
        assert result is not None
        assert result[0] == "DESCRIPTIVE_NAME"
        assert "descriptive" in result[1].lower()

    def test_valid_agent_name_returns_none(self):
        """Valid agent names should return None (no mistake detected)."""
        assert _detect_agent_name_mistake("GreenLake") is None
        assert _detect_agent_name_mistake("BlueDog") is None
        assert _detect_agent_name_mistake("RedStone") is None


class TestDetectSuspiciousFileReservation:
    """Tests for _detect_suspicious_file_reservation helper."""

    def test_overly_broad_patterns_detected(self):
        """Overly broad patterns should be flagged."""
        broad_patterns = ["*", "**", "**/*", "**/**", "."]
        for pattern in broad_patterns:
            result = _detect_suspicious_file_reservation(pattern)
            assert result is not None, f"Should detect '{pattern}' as too broad"
            assert "too broad" in result.lower()

    def test_absolute_paths_detected(self):
        """Absolute paths should be flagged."""
        result = _detect_suspicious_file_reservation("/home/user/project/src/file.py")
        assert result is not None
        assert "absolute path" in result.lower()

    def test_short_wildcard_patterns_detected(self):
        """Very short wildcard patterns should be flagged."""
        short_patterns = ["*", "*."]
        for pattern in short_patterns:
            result = _detect_suspicious_file_reservation(pattern)
            assert result is not None

    def test_valid_patterns_not_detected(self):
        """Valid specific patterns should not be flagged."""
        valid_patterns = [
            "src/api/*.py",
            "lib/auth/**",
            "tests/test_*.py",
            "src/module.py",
            "config/settings.yaml",
        ]
        for pattern in valid_patterns:
            result = _detect_suspicious_file_reservation(pattern)
            assert result is None, f"'{pattern}' should not be flagged"

    def test_double_slash_path_not_flagged_as_absolute(self):
        """Paths starting with // should not be flagged as absolute paths."""
        # This could be a UNC path or intentional
        result = _detect_suspicious_file_reservation("//some/pattern")
        # Should not trigger absolute path warning
        assert result is None or "absolute" not in result.lower()
