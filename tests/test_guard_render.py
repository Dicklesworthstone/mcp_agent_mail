from pathlib import Path
from typing import cast

from mcp_agent_mail.guard import render_precommit_script, render_prepush_script
from mcp_agent_mail.storage import ProjectArchive


class _DummyArchive:
    def __init__(self, root: Path) -> None:
        self.root = root


def test_precommit_script_contains_gate_and_mode(tmp_path: Path) -> None:
    script = render_precommit_script(cast(ProjectArchive, _DummyArchive(tmp_path)))
    assert "WORKTREES_ENABLED" in script
    assert "AGENT_MAIL_GUARD_MODE" in script
    assert "git\",\"diff\",\"--cached\",\"--name-status\",\"-M\",\"-z\"" in script


def test_prepush_script_contains_gate_and_mode(tmp_path: Path) -> None:
    script = render_prepush_script(cast(ProjectArchive, _DummyArchive(tmp_path)))
    assert "WORKTREES_ENABLED" in script
    assert "AGENT_MAIL_GUARD_MODE" in script
    assert "--no-ext-diff" in script
