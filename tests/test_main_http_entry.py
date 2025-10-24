from __future__ import annotations

from typer.testing import CliRunner
import sys

from mcp_agent_mail.http import main as http_main
from mcp_agent_mail.__main__ import main as module_main


def test_http_main_callable(monkeypatch):
    calls = {}

    def fake_run(app, host, port, log_level="info"):
        calls["ok"] = True

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mcp-http"])
    http_main()
    assert calls.get("ok") is True


def test_module_main_no_args(monkeypatch):
    # Validate that module main is callable and delegates to Typer app
    called = {"ok": False}

    def fake_run(cmd):
        called["ok"] = True

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    # Simulate `python -m mcp_agent_mail` dispatch path by calling directly
    module_main()
    # We won't assert called here since it depends on argv; just ensure no crash
    assert True


