"""Tests for tool filtering and push notification features."""

import json

import pytest


class TestToolFilter:
    """Tests for TOOLS_FILTER_ENABLED feature."""

    def test_tool_filter_settings_default_disabled(self, isolated_env, monkeypatch):
        """Tool filtering is disabled by default."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        clear_settings_cache()
        settings = get_settings()

        assert settings.tool_filter.enabled is False
        assert settings.tool_filter.profile == "full"

    def test_tool_filter_settings_enabled_with_profile(self, isolated_env, monkeypatch):
        """Tool filtering can be enabled with a profile."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
        monkeypatch.setenv("TOOLS_FILTER_PROFILE", "minimal")
        clear_settings_cache()

        settings = get_settings()
        assert settings.tool_filter.enabled is True
        assert settings.tool_filter.profile == "minimal"

    def test_tool_filter_settings_custom_mode(self, isolated_env, monkeypatch):
        """Tool filtering supports custom mode with clusters/tools lists."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
        monkeypatch.setenv("TOOLS_FILTER_PROFILE", "custom")
        monkeypatch.setenv("TOOLS_FILTER_MODE", "include")
        monkeypatch.setenv("TOOLS_FILTER_CLUSTERS", "messaging,identity")
        monkeypatch.setenv("TOOLS_FILTER_TOOLS", "health_check")
        clear_settings_cache()

        settings = get_settings()
        assert settings.tool_filter.enabled is True
        assert settings.tool_filter.profile == "custom"
        assert settings.tool_filter.mode == "include"
        assert "messaging" in settings.tool_filter.clusters
        assert "identity" in settings.tool_filter.clusters
        assert "health_check" in settings.tool_filter.tools

    def test_should_expose_tool_disabled_filter(self, isolated_env):
        """When filtering is disabled, all tools should be exposed."""
        from mcp_agent_mail.app import _should_expose_tool
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        clear_settings_cache()
        settings = get_settings()

        # With filtering disabled, all tools should be exposed
        assert _should_expose_tool("any_tool", "any_cluster", settings) is True

    def test_should_expose_tool_minimal_profile(self, isolated_env, monkeypatch):
        """Minimal profile only exposes essential tools."""
        from mcp_agent_mail.app import _should_expose_tool
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
        monkeypatch.setenv("TOOLS_FILTER_PROFILE", "minimal")
        clear_settings_cache()

        settings = get_settings()

        # Minimal profile includes these tools
        assert _should_expose_tool("health_check", "infrastructure", settings) is True
        assert _should_expose_tool("ensure_project", "infrastructure", settings) is True
        assert _should_expose_tool("register_agent", "identity", settings) is True
        assert _should_expose_tool("send_message", "messaging", settings) is True
        assert _should_expose_tool("fetch_inbox", "messaging", settings) is True

        # Minimal profile excludes these tools
        assert _should_expose_tool("search_messages", "search", settings) is False
        assert _should_expose_tool("file_reservation_paths", "file_reservations", settings) is False

    def test_should_expose_tool_custom_include(self, isolated_env, monkeypatch):
        """Custom profile with include mode only exposes specified clusters/tools."""
        from mcp_agent_mail.app import _should_expose_tool
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
        monkeypatch.setenv("TOOLS_FILTER_PROFILE", "custom")
        monkeypatch.setenv("TOOLS_FILTER_MODE", "include")
        monkeypatch.setenv("TOOLS_FILTER_CLUSTERS", "messaging")
        monkeypatch.setenv("TOOLS_FILTER_TOOLS", "health_check")
        clear_settings_cache()

        settings = get_settings()

        # Included by cluster
        assert _should_expose_tool("send_message", "messaging", settings) is True
        # Included by tool name
        assert _should_expose_tool("health_check", "infrastructure", settings) is True
        # Not in either list
        assert _should_expose_tool("file_reservation_paths", "file_reservations", settings) is False

    def test_should_expose_tool_custom_exclude(self, isolated_env, monkeypatch):
        """Custom profile with exclude mode hides specified clusters/tools."""
        from mcp_agent_mail.app import _should_expose_tool
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
        monkeypatch.setenv("TOOLS_FILTER_PROFILE", "custom")
        monkeypatch.setenv("TOOLS_FILTER_MODE", "exclude")
        monkeypatch.setenv("TOOLS_FILTER_CLUSTERS", "file_reservations,build_slots")
        clear_settings_cache()

        settings = get_settings()

        # Not in exclude list
        assert _should_expose_tool("send_message", "messaging", settings) is True
        assert _should_expose_tool("health_check", "infrastructure", settings) is True
        # In exclude list
        assert _should_expose_tool("file_reservation_paths", "file_reservations", settings) is False
        assert _should_expose_tool("acquire_build_slot", "build_slots", settings) is False


class TestNotifications:
    """Tests for NOTIFICATIONS_ENABLED feature."""

    def test_notification_settings_default_disabled(self, isolated_env):
        """Push notifications are disabled by default."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        clear_settings_cache()
        settings = get_settings()

        assert settings.notifications.enabled is False

    def test_notification_settings_enabled(self, isolated_env, monkeypatch):
        """Push notifications can be enabled via env."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings

        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", "/tmp/test_signals")
        clear_settings_cache()

        settings = get_settings()
        assert settings.notifications.enabled is True
        assert settings.notifications.signals_dir == "/tmp/test_signals"
        assert settings.notifications.include_metadata is True
        assert settings.notifications.debounce_ms == 100

    @pytest.mark.asyncio
    async def test_emit_notification_signal_disabled(self, isolated_env):
        """Signal emission is no-op when disabled."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings
        from mcp_agent_mail.storage import emit_notification_signal

        clear_settings_cache()
        settings = get_settings()

        result = await emit_notification_signal(
            settings, "test_project", "TestAgent", {"id": 1, "subject": "Test"}
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_emit_notification_signal_creates_file(self, isolated_env, monkeypatch, tmp_path):
        """Signal emission creates signal file when enabled."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings
        from mcp_agent_mail.storage import emit_notification_signal

        signals_dir = tmp_path / "signals"
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(signals_dir))
        monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_MS", "0")  # Disable debounce for test
        clear_settings_cache()

        settings = get_settings()
        result = await emit_notification_signal(
            settings,
            "test_project",
            "TestAgent",
            {"id": 123, "from": "Sender", "subject": "Hello", "importance": "high"},
        )

        assert result is True

        # Verify signal file exists
        signal_path = signals_dir / "projects" / "test_project" / "agents" / "TestAgent.signal"
        assert signal_path.exists()

        # Verify content
        data = json.loads(signal_path.read_text())
        assert data["project"] == "test_project"
        assert data["agent"] == "TestAgent"
        assert data["message"]["id"] == 123
        assert data["message"]["subject"] == "Hello"
        assert data["message"]["importance"] == "high"

    @pytest.mark.asyncio
    async def test_clear_notification_signal(self, isolated_env, monkeypatch, tmp_path):
        """Clear signal removes the signal file."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings
        from mcp_agent_mail.storage import clear_notification_signal, emit_notification_signal

        signals_dir = tmp_path / "signals"
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(signals_dir))
        monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_MS", "0")
        clear_settings_cache()

        settings = get_settings()

        # First emit a signal
        await emit_notification_signal(settings, "test_project", "TestAgent", {"id": 1})

        signal_path = signals_dir / "projects" / "test_project" / "agents" / "TestAgent.signal"
        assert signal_path.exists()

        # Now clear it
        result = await clear_notification_signal(settings, "test_project", "TestAgent")
        assert result is True
        assert not signal_path.exists()

    def test_list_pending_signals(self, isolated_env, monkeypatch, tmp_path):
        """List pending signals returns all signal files."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings
        from mcp_agent_mail.storage import list_pending_signals

        signals_dir = tmp_path / "signals"
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(signals_dir))
        clear_settings_cache()

        settings = get_settings()

        # Create some signal files manually
        proj1_dir = signals_dir / "projects" / "proj1" / "agents"
        proj1_dir.mkdir(parents=True)
        (proj1_dir / "Agent1.signal").write_text(
            json.dumps({"project": "proj1", "agent": "Agent1", "timestamp": "2024-01-01T00:00:00Z"})
        )
        (proj1_dir / "Agent2.signal").write_text(
            json.dumps({"project": "proj1", "agent": "Agent2", "timestamp": "2024-01-01T00:00:00Z"})
        )

        proj2_dir = signals_dir / "projects" / "proj2" / "agents"
        proj2_dir.mkdir(parents=True)
        (proj2_dir / "Agent3.signal").write_text(
            json.dumps({"project": "proj2", "agent": "Agent3", "timestamp": "2024-01-01T00:00:00Z"})
        )

        # List all signals
        signals = list_pending_signals(settings)
        assert len(signals) == 3

        # List signals for specific project
        signals = list_pending_signals(settings, "proj1")
        assert len(signals) == 2
        assert all(s["project"] == "proj1" for s in signals)

    @pytest.mark.asyncio
    async def test_signal_debounce(self, isolated_env, monkeypatch, tmp_path):
        """Signal emission respects debounce window."""
        from mcp_agent_mail.config import clear_settings_cache, get_settings
        from mcp_agent_mail.storage import _SIGNAL_DEBOUNCE, emit_notification_signal

        signals_dir = tmp_path / "signals"
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(signals_dir))
        monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_MS", "10000")  # 10 seconds
        clear_settings_cache()

        # Clear debounce state
        _SIGNAL_DEBOUNCE.clear()

        settings = get_settings()

        # First signal should succeed
        result1 = await emit_notification_signal(settings, "proj", "Agent", {"id": 1})
        assert result1 is True

        # Second signal within debounce window should be skipped
        result2 = await emit_notification_signal(settings, "proj", "Agent", {"id": 2})
        assert result2 is False

        # Different agent should still work
        result3 = await emit_notification_signal(settings, "proj", "OtherAgent", {"id": 3})
        assert result3 is True
