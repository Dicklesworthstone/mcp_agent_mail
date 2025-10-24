from __future__ import annotations

import contextlib

import pytest

from mcp_agent_mail import config as _config
from mcp_agent_mail.llm import _bridge_provider_env, complete_system_user
from mcp_agent_mail.utils import generate_agent_name, sanitize_agent_name, slugify


def test_utils_functions_basic():
    assert slugify(" My Project ") == "my-project"
    assert sanitize_agent_name(" A!@#gent 123 ") == "Agent123"
    # sanitize removes non-alnum and can return None when empty
    assert sanitize_agent_name("@@@") is None
    name = generate_agent_name()
    assert isinstance(name, str) and len(name) > 0


def test_bridge_provider_env_populates_from_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-123")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    _bridge_provider_env()
    import os

    assert os.environ.get("GOOGLE_API_KEY") == "gem-123"


@pytest.mark.asyncio
async def test_complete_system_user_handles_missing_router(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "dummy-model")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    import litellm as ll

    class _Resp:
        model = "dummy"
        provider = None

        class _Choice:
            def __init__(self):
                self.message = {"content": "hello"}

        def __init__(self):
            self.choices = [self._Choice()]

    class _Router:
        def __init__(self, *a, **k):
            raise RuntimeError("no router available")

    monkeypatch.setattr(ll, "Router", _Router, raising=False)

    def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        return _Resp()

    monkeypatch.setattr(ll, "completion", fake_completion, raising=False)

    out = await complete_system_user("sys", "user")
    assert out.content == "hello"


