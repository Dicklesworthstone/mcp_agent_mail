from pathlib import Path

from mcp_agent_mail.guard import render_precommit_script, render_prepush_script


class _DummyArchive:
    def __init__(self, root: Path) -> None:
        self.root = root


def test_precommit_script_contains_gate_and_mode(tmp_path: Path) -> None:
    script = render_precommit_script(_DummyArchive(tmp_path))  # type: ignore[arg-type]
    assert "WORKTREES_ENABLED" in script
    assert "AGENT_MAIL_GUARD_MODE" in script
    assert "git\",\"diff\",\"--cached\",\"--name-status\",\"-M\",\"-z\"" in script


def test_prepush_script_contains_gate_and_mode(tmp_path: Path) -> None:
    script = render_prepush_script(_DummyArchive(tmp_path))  # type: ignore[arg-type]
    assert "WORKTREES_ENABLED" in script
    assert "AGENT_MAIL_GUARD_MODE" in script
    assert "--no-ext-diff" in script

