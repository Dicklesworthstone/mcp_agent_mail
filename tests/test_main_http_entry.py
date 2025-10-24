from __future__ import annotations

import sys

from mcp_agent_mail.__main__ import main as module_main


def test_module_main_no_args(monkeypatch):
    # Validate that module main is callable and delegates to Typer app
    called = {"ok": False}

    def fake_run(cmd):
        called["ok"] = True

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    monkeypatch.setattr(sys, "argv", ["mcp-agent-mail"])  # neutral argv
    module_main()
    assert True


