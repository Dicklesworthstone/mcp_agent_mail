"""Tests for single-server ownership of STORAGE_ROOT via server.lock (issue #123)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from filelock import FileLock
from typer.testing import CliRunner

from mcp_agent_mail.cli import _SERVER_LOCK_FILENAME, _acquire_server_lock, app


def test_acquire_server_lock_creates_lockfile(isolated_env, tmp_path, monkeypatch):
    """_acquire_server_lock creates server.lock and writes PID to server.pid."""
    storage_root = tmp_path / "lock_test_storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    lock = _acquire_server_lock()
    try:
        lock_path = storage_root / _SERVER_LOCK_FILENAME
        assert lock_path.exists()
        pid_path = storage_root / "server.pid"
        assert pid_path.exists()
        pid_text = pid_path.read_text(encoding="utf-8").strip()
        assert pid_text == str(os.getpid())
    finally:
        lock.release()


def test_acquire_server_lock_blocks_second_acquisition(isolated_env, tmp_path, monkeypatch):
    """Second call to _acquire_server_lock exits with code 1."""
    storage_root = tmp_path / "lock_test_storage2"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()

    # Hold the lock manually using FileLock (OS-level, same as production)
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / _SERVER_LOCK_FILENAME
    holder = FileLock(str(lock_path))
    holder.acquire(timeout=0)
    # Write PID to companion file (same as production code)
    pid_path = storage_root / "server.pid"
    pid_path.write_text("99999", encoding="utf-8")
    try:
        # Second acquisition should fail with SystemExit(1)
        import pytest

        with pytest.raises(SystemExit) as exc_info:
            _acquire_server_lock()
        assert exc_info.value.code == 1
    finally:
        holder.release()


def test_serve_http_acquires_lock(isolated_env, tmp_path, monkeypatch):
    """serve-http acquires the server lock before running uvicorn."""
    storage_root = tmp_path / "http_lock_storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_uvicorn_run(app: Any, **kwargs: Any) -> None:
        call_args.update(kwargs)
        # Verify the lockfile exists while the server is "running"
        lock_path = storage_root / _SERVER_LOCK_FILENAME
        assert lock_path.exists(), "server.lock should exist while server is running"

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    result = runner.invoke(app, ["serve-http"])
    assert result.exit_code == 0


def test_serve_http_fails_when_locked(isolated_env, tmp_path, monkeypatch):
    """serve-http fails immediately when another server holds the lock."""
    storage_root = tmp_path / "http_lock_blocked"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / _SERVER_LOCK_FILENAME
    holder = FileLock(str(lock_path))
    holder.acquire(timeout=0)
    pid_path = storage_root / "server.pid"
    pid_path.write_text("12345", encoding="utf-8")
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["serve-http"])
        assert result.exit_code == 1
    finally:
        holder.release()


def test_serve_stdio_acquires_lock(isolated_env, tmp_path, monkeypatch):
    """serve-stdio acquires the server lock before starting."""
    storage_root = tmp_path / "stdio_lock_storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_run(self: Any, transport: str = "stdio", **kwargs: Any) -> None:
        call_args["transport"] = transport
        lock_path = storage_root / _SERVER_LOCK_FILENAME
        assert lock_path.exists(), "server.lock should exist while server is running"

    from fastmcp import FastMCP

    monkeypatch.setattr(FastMCP, "run", fake_run)
    result = runner.invoke(app, ["serve-stdio"])
    assert result.exit_code == 0


def test_serve_stdio_fails_when_locked(isolated_env, tmp_path, monkeypatch):
    """serve-stdio fails immediately when another server holds the lock."""
    storage_root = tmp_path / "stdio_lock_blocked"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / _SERVER_LOCK_FILENAME
    holder = FileLock(str(lock_path))
    holder.acquire(timeout=0)
    pid_path = storage_root / "server.pid"
    pid_path.write_text("67890", encoding="utf-8")
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["serve-stdio"])
        assert result.exit_code == 1
    finally:
        holder.release()


def test_lock_released_on_process_exit(isolated_env, tmp_path, monkeypatch):
    """After _acquire_server_lock holder is released, a new acquisition succeeds."""
    storage_root = tmp_path / "lock_release_test"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    lock1 = _acquire_server_lock()
    lock1.release()
    # Second acquisition should succeed after release
    clear_settings_cache()
    lock2 = _acquire_server_lock()
    try:
        lock_path = storage_root / _SERVER_LOCK_FILENAME
        assert lock_path.exists()
    finally:
        lock2.release()


def test_error_message_includes_pid(isolated_env, tmp_path, monkeypatch, capsys):
    """Error message includes the PID of the process holding the lock."""
    storage_root = tmp_path / "pid_msg_test"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / _SERVER_LOCK_FILENAME
    holder = FileLock(str(lock_path))
    holder.acquire(timeout=0)
    # Write PID to companion file
    pid_path = storage_root / "server.pid"
    pid_path.write_text("42", encoding="utf-8")
    try:
        import pytest

        with pytest.raises(SystemExit):
            _acquire_server_lock()
        captured = capsys.readouterr()
        assert "42" in captured.err
        assert "Another Agent Mail server is already running" in captured.err
    finally:
        holder.release()
