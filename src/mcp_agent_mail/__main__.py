"""Allow `python -m mcp_agent_mail` to invoke the CLI entry-point."""

from typer.main import get_command

from .cli import app


def main() -> None:
    """Dispatch to the Typer CLI without inheriting external argv flags."""
    cmd = get_command(app)
    # Run help to avoid UsageError and to avoid pytest args leakage
    cmd.main(args=["--help"], prog_name="mcp-agent-mail", standalone_mode=False)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
