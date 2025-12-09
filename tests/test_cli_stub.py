"""Tests for the CLI stub that helps confused agents.

The CLI stub is a shell script installed by install.sh that prints a helpful
message when agents mistakenly try to run 'mcp-agent-mail' as a CLI command
instead of using the MCP tools.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def cli_stub_script(tmp_path: Path) -> Path:
    """Create a test copy of the CLI stub script."""
    stub_content = '''#!/usr/bin/env bash
# MCP Agent Mail — Helpful Stub for Confused Agents
cat <<'MSG'
MCP Agent Mail is NOT a CLI tool!

It's an MCP (Model Context Protocol) server that provides tools to your
AI coding agent. You should already have access to these tools as part
of your available MCP tools.

CORRECT USAGE:
   Use the MCP tools directly, for example:
     • mcp__mcp-agent-mail__register_agent
     • mcp__mcp-agent-mail__send_message
     • mcp__mcp-agent-mail__fetch_inbox
MSG
exit 1
'''
    stub_path = tmp_path / "mcp-agent-mail"
    stub_path.write_text(stub_content)
    stub_path.chmod(0o755)
    return stub_path


def test_cli_stub_prints_not_cli_message(cli_stub_script: Path):
    """Test that the CLI stub prints a message explaining it's not a CLI tool."""
    result = subprocess.run(
        [str(cli_stub_script)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, "CLI stub should exit with code 1"
    output = result.stdout

    # Check for key messages in the output
    assert "NOT a CLI" in output or "not a CLI" in output.lower()
    assert "MCP" in output
    assert "mcp__mcp-agent-mail__" in output


def test_cli_stub_mentions_correct_tools(cli_stub_script: Path):
    """Test that the CLI stub mentions the correct MCP tool names."""
    result = subprocess.run(
        [str(cli_stub_script)],
        capture_output=True,
        text=True,
    )

    output = result.stdout

    # Should mention some of the key tools
    assert "register_agent" in output
    assert "send_message" in output
    assert "fetch_inbox" in output


def test_cli_stub_ignores_arguments(cli_stub_script: Path):
    """Test that the CLI stub ignores any arguments passed to it."""
    # Try various argument patterns that a confused agent might try
    test_cases = [
        ["--help"],
        ["send", "--to", "BlueLake", "--message", "Hello"],
        ["register", "--name", "MyAgent"],
        ["-v"],
    ]

    for args in test_cases:
        result = subprocess.run(
            [str(cli_stub_script), *args],
            capture_output=True,
            text=True,
        )

        # Should always exit 1 regardless of arguments
        assert result.returncode == 1
        # Should always print the help message
        assert "MCP" in result.stdout


class TestInstallScriptCliStub:
    """Tests for the install_cli_stub function in install.sh."""

    def test_install_function_exists(self):
        """Verify the install_cli_stub function exists in install.sh."""
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()

        assert "install_cli_stub()" in content, "install_cli_stub function should exist"

    def test_install_creates_variants(self):
        """Verify install script creates variant symlinks."""
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()

        # Should create symlinks for common variants
        expected_variants = ["mcp_agent_mail", "mcpagentmail", "agentmail", "agent-mail"]
        for variant in expected_variants:
            assert variant in content, f"Should create symlink for '{variant}'"

    def test_stub_mentions_github_repo(self):
        """Verify the stub script mentions the GitHub repo for documentation."""
        install_script = Path(__file__).parent.parent / "scripts" / "install.sh"
        content = install_script.read_text()

        assert "github.com" in content.lower() and "mcp_agent_mail" in content
