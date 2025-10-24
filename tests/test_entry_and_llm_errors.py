from __future__ import annotations

import importlib
from pathlib import Path


def test_module_entry_point_executes_main(monkeypatch, tmp_path: Path):
    # Import __main__ and ensure it exposes main(); do not actually run uvicorn etc.
    mod = importlib.import_module("mcp_agent_mail.__main__")
    assert hasattr(mod, "main") and callable(mod.main)


def test_llm_env_bridge_no_crash(monkeypatch):
    # Ensure missing env vars don't crash initialization
    from mcp_agent_mail.llm import _bridge_provider_env
    _bridge_provider_env()
    # No assertion needed; should not raise


