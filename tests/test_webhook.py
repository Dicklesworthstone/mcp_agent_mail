"""Tests for webhook functionality.

Tests cover:
- _infer_platform(): Platform detection from program/model
- _get_webhook_command(): Command resolution with priority chain
- _fire_webhook(): Webhook execution with placeholder substitution
- _log_webhook_result(): Subprocess result logging and cleanup
- Integration: Webhooks firing on message delivery
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_agent_mail.app import (
    DEFAULT_WEBHOOK_PLATFORM,
    WEBHOOK_PLATFORM_COMMANDS,
    _get_webhook_command,
    _infer_platform,
    _log_webhook_result,
)


# =============================================================================
# Tests for _infer_platform()
# =============================================================================


class TestInferPlatform:
    """Tests for _infer_platform() function."""

    def test_infers_claude_from_program(self):
        """Claude in program name returns 'claude'."""
        assert _infer_platform("claude", "") == "claude"
        assert _infer_platform("Claude", "") == "claude"
        assert _infer_platform("claude-code", "") == "claude"

    def test_infers_claude_from_model(self):
        """Claude in model name returns 'claude'."""
        assert _infer_platform("", "claude-3-opus") == "claude"
        assert _infer_platform("", "Claude-Sonnet") == "claude"

    def test_infers_gemini_from_program(self):
        """Gemini in program name returns 'gemini'."""
        assert _infer_platform("gemini", "") == "gemini"
        assert _infer_platform("Gemini", "") == "gemini"
        assert _infer_platform("gemini-cli", "") == "gemini"

    def test_infers_gemini_from_model(self):
        """Gemini in model name returns 'gemini'."""
        assert _infer_platform("", "gemini-pro") == "gemini"
        assert _infer_platform("", "Gemini-Ultra") == "gemini"

    def test_infers_codex_from_program(self):
        """Codex in program name returns 'codex'."""
        assert _infer_platform("codex", "") == "codex"
        assert _infer_platform("Codex", "") == "codex"

    def test_infers_codex_from_model(self):
        """Codex in model name returns 'codex'."""
        assert _infer_platform("", "codex-v1") == "codex"

    def test_infers_codex_from_openai(self):
        """OpenAI models map to codex platform."""
        assert _infer_platform("", "gpt-4") == "codex"
        assert _infer_platform("", "gpt-4-turbo") == "codex"
        assert _infer_platform("", "openai-gpt") == "codex"
        assert _infer_platform("openai", "") == "codex"

    def test_returns_none_for_unknown(self):
        """Unknown program/model returns None."""
        assert _infer_platform("", "") is None
        assert _infer_platform("unknown", "unknown-model") is None
        assert _infer_platform("custom-cli", "custom-model") is None

    def test_combined_detection(self):
        """Platform detected from combined program+model."""
        # Program takes priority in combined check order
        assert _infer_platform("my-claude-wrapper", "custom") == "claude"
        assert _infer_platform("custom", "uses-gemini-pro") == "gemini"


# =============================================================================
# Tests for _get_webhook_command()
# =============================================================================


class TestGetWebhookCommand:
    """Tests for _get_webhook_command() function."""

    def _make_settings(
        self,
        webhook_command: str | None = None,
        webhook_platform: str | None = None,
    ) -> MagicMock:
        """Create a mock Settings object with specified webhook config."""
        settings = MagicMock()
        settings.webhook_enabled = True
        settings.webhook_command = webhook_command
        settings.webhook_platform = webhook_platform
        return settings

    def test_custom_command_takes_priority(self):
        """Custom WEBHOOK_COMMAND overrides everything."""
        settings = self._make_settings(
            webhook_command="my-custom-command {recipient}",
            webhook_platform="gemini",
        )
        result = _get_webhook_command(settings, agent_platform="codex")
        assert result == "my-custom-command {recipient}"

    def test_agent_platform_used_when_no_custom_command(self):
        """Agent's platform used when no custom command set."""
        settings = self._make_settings(webhook_platform="gemini")
        result = _get_webhook_command(settings, agent_platform="codex")
        assert result == WEBHOOK_PLATFORM_COMMANDS["codex"]

    def test_agent_platform_case_insensitive(self):
        """Agent platform matching is case-insensitive."""
        settings = self._make_settings()
        result = _get_webhook_command(settings, agent_platform="CLAUDE")
        assert result == WEBHOOK_PLATFORM_COMMANDS["claude"]

    def test_global_platform_fallback(self):
        """Falls back to global WEBHOOK_PLATFORM when agent has none."""
        settings = self._make_settings(webhook_platform="gemini")
        result = _get_webhook_command(settings, agent_platform=None)
        assert result == WEBHOOK_PLATFORM_COMMANDS["gemini"]

    def test_global_platform_case_insensitive(self):
        """Global platform matching is case-insensitive."""
        settings = self._make_settings(webhook_platform="CODEX")
        result = _get_webhook_command(settings, agent_platform=None)
        assert result == WEBHOOK_PLATFORM_COMMANDS["codex"]

    def test_defaults_to_claude(self):
        """Defaults to claude when no platform configured."""
        settings = self._make_settings()
        result = _get_webhook_command(settings, agent_platform=None)
        assert result == WEBHOOK_PLATFORM_COMMANDS["claude"]
        assert DEFAULT_WEBHOOK_PLATFORM == "claude"

    def test_invalid_agent_platform_falls_back(self):
        """Invalid agent platform falls back to global/default."""
        settings = self._make_settings(webhook_platform="gemini")
        result = _get_webhook_command(settings, agent_platform="invalid-platform")
        assert result == WEBHOOK_PLATFORM_COMMANDS["gemini"]

    def test_invalid_global_platform_returns_none(self):
        """Invalid global platform with no agent platform returns None."""
        settings = self._make_settings(webhook_platform="invalid")
        result = _get_webhook_command(settings, agent_platform=None)
        assert result is None

    def test_all_platform_commands_exist(self):
        """All platform commands have valid entries."""
        settings = self._make_settings()
        for platform in ["claude", "gemini", "codex", "cursor"]:
            result = _get_webhook_command(settings, agent_platform=platform)
            assert result is not None
            assert "{project}" in result
            assert "{recipient}" in result


# =============================================================================
# Tests for _log_webhook_result()
# =============================================================================


class TestLogWebhookResult:
    """Tests for _log_webhook_result() function."""

    @pytest.mark.asyncio
    async def test_logs_success_on_zero_exit_code(self):
        """Successful subprocess logs info message."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"output", b""))
        proc.returncode = 0

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            await _log_webhook_result("test-agent", proc)
            mock_logger.info.assert_called()
            assert "completed successfully" in str(mock_logger.info.call_args)

    @pytest.mark.asyncio
    async def test_logs_warning_on_nonzero_exit_code(self):
        """Failed subprocess logs warning with stderr."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"error message"))
        proc.returncode = 1

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            await _log_webhook_result("test-agent", proc)
            mock_logger.warning.assert_called()
            call_str = str(mock_logger.warning.call_args)
            assert "exited with code 1" in call_str
            assert "error message" in call_str

    @pytest.mark.asyncio
    async def test_truncates_long_stderr(self):
        """Long stderr is truncated to 500 chars."""
        proc = AsyncMock()
        long_error = "x" * 1000
        proc.communicate = AsyncMock(return_value=(b"", long_error.encode()))
        proc.returncode = 1

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            await _log_webhook_result("test-agent", proc)
            call_str = str(mock_logger.warning.call_args)
            # Should be truncated to 500 chars
            assert "x" * 500 in call_str
            assert "x" * 501 not in call_str

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Timeout after 30s kills process and logs."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            await _log_webhook_result("test-agent", proc)
            proc.kill.assert_called_once()
            await proc.wait()
            mock_logger.info.assert_called()
            assert "timed out" in str(mock_logger.info.call_args)

    @pytest.mark.asyncio
    async def test_handles_exception_and_cleans_up(self):
        """General exception logs warning and cleans up process."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=Exception("Unexpected error"))
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            await _log_webhook_result("test-agent", proc)
            proc.kill.assert_called_once()
            mock_logger.warning.assert_called()
            assert "failed" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_handles_kill_exception_gracefully(self):
        """Exception during kill is suppressed."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock(side_effect=ProcessLookupError())
        proc.wait = AsyncMock()

        # Should not raise
        with patch("mcp_agent_mail.app.logger"):
            await _log_webhook_result("test-agent", proc)


# =============================================================================
# Tests for _fire_webhook()
# =============================================================================


class TestFireWebhook:
    """Tests for _fire_webhook() function.

    Note: These tests use integration-style testing because _fire_webhook
    requires database access. Direct unit tests with mocked DB would be
    too fragile.
    """

    @pytest.mark.asyncio
    async def test_disabled_webhook_returns_early(self, isolated_env, monkeypatch):
        """When webhook_enabled=false, returns immediately without action."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "false")

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        with patch("mcp_agent_mail.app.asyncio.create_subprocess_shell") as mock_shell:
            await _fire_webhook(["agent1"], "/project", {"id": "msg1"})
            mock_shell.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_project(self, isolated_env, monkeypatch):
        """Logs warning and returns when project not found."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        with patch("mcp_agent_mail.app.logger") as mock_logger:
            with patch(
                "mcp_agent_mail.app.asyncio.create_subprocess_shell"
            ) as mock_shell:
                await _fire_webhook(
                    ["Agent"],
                    "/nonexistent-project",
                    {"id": "1"},
                )
                mock_shell.assert_not_called()
                mock_logger.warning.assert_called()
                assert "failed to look up project" in str(
                    mock_logger.warning.call_args
                ).lower()


# =============================================================================
# Integration Tests
# =============================================================================


class TestWebhookIntegration:
    """Integration tests for webhook triggering on message delivery.

    These tests verify that:
    1. _fire_webhook is called with correct arguments when messages are sent
    2. Subprocess execution works correctly (mocked)
    3. Multiple recipients each get webhook calls
    4. Placeholder substitution works in real flow
    """

    @pytest.mark.asyncio
    async def test_send_message_fires_webhook_for_recipients(
        self, isolated_env, monkeypatch
    ):
        """send_message triggers webhook for each recipient."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_PLATFORM", "claude")

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        from fastmcp import Client

        server = build_mcp_server()

        webhook_calls = []

        async def mock_fire_webhook(recipients, project_key, payload):
            webhook_calls.append({
                "recipients": list(recipients),
                "project_key": project_key,
                "payload": dict(payload),
            })

        with patch("mcp_agent_mail.app._fire_webhook", side_effect=mock_fire_webhook):
            async with Client(server) as client:
                await client.call_tool("ensure_project", {"human_key": "/proj"})
                # Use valid agent names that the system accepts
                sender = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "BlueLake",
                        "task_description": "sending",
                    },
                )
                sender_name = sender.data["name"]

                receiver1 = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "GreenForest",
                        "task_description": "receiving",
                    },
                )
                receiver1_name = receiver1.data["name"]

                receiver2 = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "RedMountain",
                        "task_description": "receiving",
                    },
                )
                receiver2_name = receiver2.data["name"]

                await client.call_tool(
                    "send_message",
                    {
                        "project_key": "/proj",
                        "sender_name": sender_name,
                        "to": [receiver1_name, receiver2_name],
                        "subject": "Hello",
                        "body_md": "Test message",
                    },
                )

        assert len(webhook_calls) >= 1
        # Check that recipients were passed
        all_recipients = []
        for call in webhook_calls:
            all_recipients.extend(call["recipients"])
        assert receiver1_name in all_recipients or receiver2_name in all_recipients

    @pytest.mark.asyncio
    async def test_send_message_passes_correct_payload(self, isolated_env, monkeypatch):
        """send_message passes message details in payload."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_PLATFORM", "claude")

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        from fastmcp import Client

        server = build_mcp_server()

        captured_payload = None
        sender_name_captured = None

        async def mock_fire_webhook(recipients, project_key, payload):
            nonlocal captured_payload
            captured_payload = dict(payload)

        with patch("mcp_agent_mail.app._fire_webhook", side_effect=mock_fire_webhook):
            async with Client(server) as client:
                await client.call_tool("ensure_project", {"human_key": "/proj"})
                sender = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "BlueLake",
                        "task_description": "sending",
                    },
                )
                sender_name_captured = sender.data["name"]

                receiver = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "GreenForest",
                        "task_description": "receiving",
                    },
                )
                receiver_name = receiver.data["name"]

                await client.call_tool(
                    "send_message",
                    {
                        "project_key": "/proj",
                        "sender_name": sender_name_captured,
                        "to": [receiver_name],
                        "subject": "Test Subject",
                        "body_md": "Test body",
                    },
                )

        assert captured_payload is not None
        assert captured_payload.get("subject") == "Test Subject"
        assert captured_payload.get("from") == sender_name_captured
        assert "id" in captured_payload

    @pytest.mark.asyncio
    async def test_reply_message_fires_webhook(self, isolated_env, monkeypatch):
        """reply_message triggers webhook for recipients."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_PLATFORM", "claude")

        from mcp_agent_mail.app import build_mcp_server
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        from fastmcp import Client

        server = build_mcp_server()

        webhook_calls = []

        async def mock_fire_webhook(recipients, project_key, payload):
            webhook_calls.append({"recipients": recipients, "payload": payload})

        with patch("mcp_agent_mail.app._fire_webhook", side_effect=mock_fire_webhook):
            async with Client(server) as client:
                await client.call_tool("ensure_project", {"human_key": "/proj"})
                alice = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "BlueLake",
                        "task_description": "alice",
                    },
                )
                alice_name = alice.data["name"]

                bob = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "/proj",
                        "program": "claude",
                        "model": "opus",
                        "name": "GreenForest",
                        "task_description": "bob",
                    },
                )
                bob_name = bob.data["name"]

                # Send initial message
                msg = await client.call_tool(
                    "send_message",
                    {
                        "project_key": "/proj",
                        "sender_name": alice_name,
                        "to": [bob_name],
                        "subject": "Hello",
                        "body_md": "Initial message",
                    },
                )

                # Get message ID for reply
                deliveries = msg.data.get("deliveries", [])
                message_id = deliveries[0]["payload"]["id"] if deliveries else None

                if message_id:
                    # Reply to message
                    await client.call_tool(
                        "reply_message",
                        {
                            "project_key": "/proj",
                            "sender_name": bob_name,
                            "message_id": message_id,
                            "body_md": "Reply message",
                        },
                    )

        # Should have webhook calls for both send and reply
        assert len(webhook_calls) >= 2

    @pytest.mark.asyncio
    async def test_webhook_subprocess_executed(self, isolated_env, monkeypatch):
        """Verifies subprocess shell command contains correct substitutions."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_COMMAND", "echo {recipient} {subject}")

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        executed_commands = []
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
        mock_proc.returncode = 0

        async def capture_command(cmd, **kwargs):
            executed_commands.append(cmd)
            return mock_proc

        # Create a mock project
        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.human_key = "/proj"

        # Create a mock agent result for the session query
        mock_agent = MagicMock()
        mock_agent.name = "BlueLake"
        mock_agent.platform = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_agent

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        message_data = {"id": "msg-123", "subject": "TestSubject"}

        with patch(
            "mcp_agent_mail.app.asyncio.create_subprocess_shell",
            side_effect=capture_command,
        ):
            with patch("mcp_agent_mail.app._get_project_by_identifier", AsyncMock(return_value=mock_project)):
                with patch("mcp_agent_mail.app.get_session", return_value=mock_session):
                    await _fire_webhook(["BlueLake"], "/proj", message_data)

        # Verify command was executed with substitutions
        assert len(executed_commands) >= 1
        cmd = executed_commands[0]
        assert "BlueLake" in cmd or "'BlueLake'" in cmd
        assert "TestSubject" in cmd or "'TestSubject'" in cmd


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestWebhookEdgeCases:
    """Edge case and error handling tests."""

    @pytest.mark.asyncio
    async def test_empty_recipients_does_not_spawn_subprocess(
        self, isolated_env, monkeypatch
    ):
        """Empty recipients list in _fire_webhook spawns no subprocess."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        # Empty recipients list - no project lookup needed
        with patch(
            "mcp_agent_mail.app.asyncio.create_subprocess_shell"
        ) as mock_shell:
            with patch("mcp_agent_mail.app._get_project_by_identifier") as mock_proj:
                mock_proj.return_value = MagicMock(id=1)
                await _fire_webhook([], "/proj", {"id": "1"})
                mock_shell.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_escapes_dangerous_characters(self, isolated_env, monkeypatch):
        """Verifies dangerous shell characters are escaped in commands."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_COMMAND", "echo {subject}")

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        executed_commands = []
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def capture_command(cmd, **kwargs):
            executed_commands.append(cmd)
            return mock_proc

        # Create a mock project
        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.human_key = "/proj"

        # Create a mock agent result for the session query
        mock_agent = MagicMock()
        mock_agent.name = "BlueLake"
        mock_agent.platform = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_agent

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Message with shell injection attempt in subject
        message_data = {"id": "msg-123", "subject": "test; rm -rf /"}

        with patch(
            "mcp_agent_mail.app.asyncio.create_subprocess_shell",
            side_effect=capture_command,
        ):
            with patch("mcp_agent_mail.app._get_project_by_identifier", AsyncMock(return_value=mock_project)):
                with patch("mcp_agent_mail.app.get_session", return_value=mock_session):
                    await _fire_webhook(["BlueLake"], "/proj", message_data)

        # Verify the dangerous command was escaped
        assert len(executed_commands) >= 1
        cmd = executed_commands[0]
        # The subject should be quoted, preventing shell injection
        assert "'" in cmd  # shlex.quote adds quotes

    def test_webhook_platform_commands_have_required_placeholders(self):
        """All platform commands contain required placeholders."""
        required_placeholders = ["{project}", "{recipient}"]
        for platform, cmd in WEBHOOK_PLATFORM_COMMANDS.items():
            for placeholder in required_placeholders:
                assert placeholder in cmd, (
                    f"Platform {platform} missing {placeholder}"
                )

    def test_default_webhook_platform_is_valid(self):
        """DEFAULT_WEBHOOK_PLATFORM is a valid platform."""
        assert DEFAULT_WEBHOOK_PLATFORM in WEBHOOK_PLATFORM_COMMANDS

    def test_all_supported_platforms_have_commands(self):
        """All supported platforms have corresponding commands."""
        expected_platforms = ["claude", "gemini", "codex", "cursor"]
        for platform in expected_platforms:
            assert platform in WEBHOOK_PLATFORM_COMMANDS, f"Missing command for {platform}"

    @pytest.mark.asyncio
    async def test_webhook_uses_agent_platform_over_global(
        self, isolated_env, monkeypatch
    ):
        """Agent's platform setting takes precedence over global setting."""
        monkeypatch.setenv("WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("WEBHOOK_PLATFORM", "claude")  # Global default

        from mcp_agent_mail.app import _fire_webhook
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()

        executed_commands = []
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def capture_command(cmd, **kwargs):
            executed_commands.append(cmd)
            return mock_proc

        # Create a mock project
        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.human_key = "/proj"

        # Create a mock agent with gemini platform set
        mock_agent = MagicMock()
        mock_agent.name = "GreenForest"
        mock_agent.platform = "gemini"  # Agent has explicit platform

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_agent

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        message_data = {"id": "msg-123", "subject": "Test"}

        with patch(
            "mcp_agent_mail.app.asyncio.create_subprocess_shell",
            side_effect=capture_command,
        ):
            with patch("mcp_agent_mail.app._get_project_by_identifier", AsyncMock(return_value=mock_project)):
                with patch("mcp_agent_mail.app.get_session", return_value=mock_session):
                    await _fire_webhook(["GreenForest"], "/proj", message_data)

        # Verify gemini command was used, not claude
        assert len(executed_commands) >= 1
        cmd = executed_commands[0]
        assert "gemini" in cmd
