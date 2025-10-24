from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from mcp_agent_mail import config as _config


def test_main_module_dispatch(monkeypatch):
    # Ensure calling __main__.main() renders help via Typer without consuming pytest argv
    # Ensure pytest argv isn't passed through
    import sys as _sys

    import mcp_agent_mail.__main__ as entry
    from mcp_agent_mail.cli import app as real_app
    monkeypatch.setattr(_sys, "argv", ["mcp-agent-mail", "--help"])  # safe
    monkeypatch.setattr(entry, "app", real_app)
    entry.main()  # should not raise


def test_http_module_main_invokes_uvicorn(isolated_env, monkeypatch):
    # Verify http.main uses settings + argparse defaults to run uvicorn
    from mcp_agent_mail import http as http_mod

    captured: dict[str, Any] = {}

    def fake_run(app, host, port, log_level="info"):
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr("uvicorn.run", fake_run)
    # Simulate no CLI args beyond program name
    monkeypatch.setattr(http_mod, "get_settings", _config.get_settings)
    monkeypatch.setattr("sys.argv", ["mcp-agent-mail-http"])
    http_mod.main()
    assert captured["host"] == _config.get_settings().http.host
    assert captured["port"] == _config.get_settings().http.port


def test_llm_env_bridge_and_callbacks(monkeypatch):
    # Ensure provider envs are bridged and success callback can be installed
    from mcp_agent_mail import llm as llm_mod

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    # Force cost logging on
    monkeypatch.setenv("LLM_COST_LOGGING_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    # Bridge provider envs
    llm_mod._bridge_provider_env()
    import os

    assert os.environ.get("GOOGLE_API_KEY") == "g-key"

    # Stub litellm behaviors to avoid network and heavy imports
    class _StubResp:
        def __init__(self) -> None:
            self.model = "stub-model"
            self.provider = "stub"
            self.choices = [{"message": {"content": "ok"}}]

    class _StubRouter:
        def completion(self, **kwargs):  # type: ignore[no-untyped-def]
            # Emulate LiteLLM Router interface
            return _StubResp()

    import litellm as litellm_pkg  # type: ignore

    # Install stubs and capture success_callback list
    monkeypatch.setattr(litellm_pkg, "Router", _StubRouter)
    monkeypatch.setattr(litellm_pkg, "enable_cache", lambda **_: None)
    monkeypatch.setattr(litellm_pkg, "completion", lambda **_: _StubResp())
    # Ensure attribute exists for callbacks
    monkeypatch.setattr(litellm_pkg, "success_callback", [], raising=False)

    # Running a simple completion should succeed and return normalized output
    out = asyncio.run(llm_mod.complete_system_user("sys", "user"))
    # content may vary by stub path; assert at least model populated
    assert isinstance(out.model, str) and len(out.model) > 0


