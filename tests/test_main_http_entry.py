from __future__ import annotations

from typer.testing import CliRunner


def test_cli_help_no_args():
    # Invoking CLI help should succeed
    runner = CliRunner()
    from mcp_agent_mail.cli import app as cli_app
    res = runner.invoke(cli_app, ["--help"])
    assert res.exit_code == 0


