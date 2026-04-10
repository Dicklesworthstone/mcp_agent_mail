import sys
from pathlib import Path

import click
import pytest

from mcp_agent_mail.cli import am_run
from mcp_agent_mail.config import get_settings


def test_am_run_creates_lease_when_enabled(tmp_path: Path, monkeypatch) -> None:
    # Point archive to a temp root and enable worktrees features
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "warn")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    # Run a trivial child that exits 0
    am_run(
        slot="unittest-slot",
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        project_path=proj,
        agent="TestAgent",
        ttl_seconds=120,
        shared=False,
    )
    # Confirm lease was created under archive build_slots
    archive_root = Path(get_settings().storage.root).expanduser().resolve()
    # We don't know the slug in advance; scan for build_slots presence
    projects_dir = archive_root / "projects"
    assert projects_dir.exists()
    # At least one project directory should have a build_slots/unittest-slot/ file inside
    found = False
    for entry in projects_dir.glob("*/build_slots/unittest-slot/*.json"):
        if entry.is_file():
            found = True
            break
    assert found, "Expected a lease JSON file to be created for am-run"


class _StaticJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_am_run_blocks_on_structured_content_conflicts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", "block")
    monkeypatch.setenv("AGENT_NAME", "TestAgent")
    get_settings.cache_clear()

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)

    def fake_post(self, url, json=None, headers=None):
        tool_name = ((json or {}).get("params") or {}).get("name")
        if tool_name == "acquire_build_slot":
            return _StaticJsonResponse(
                {
                    "jsonrpc": "2.0",
                    "id": "am-run-acquire",
                    "result": {
                        "structuredContent": {
                            "conflicts": [
                                {
                                    "slot": "unittest-slot",
                                    "agent": "OtherAgent",
                                    "branch": "main",
                                    "expires_ts": "2026-04-10T03:00:00Z",
                                }
                            ]
                        }
                    },
                }
            )
        return _StaticJsonResponse({"jsonrpc": "2.0", "id": "ok", "result": {"structuredContent": {}}})

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not execute when server reports a build-slot conflict")

    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr("subprocess.run", unexpected_run)

    with pytest.raises(click.exceptions.Exit) as excinfo:
        am_run(
            slot="unittest-slot",
            cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            project_path=proj,
            agent="TestAgent",
            ttl_seconds=120,
            shared=False,
            block_on_conflicts=True,
        )

    assert excinfo.value.exit_code == 1
