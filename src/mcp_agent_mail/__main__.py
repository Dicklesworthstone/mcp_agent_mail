"""Allow `python -m mcp_agent_mail` to invoke the CLI entry-point."""

from typer.main import get_command

from .cli import app


def main() -> None:
    """Dispatch to the Typer CLI entry-point, tolerating external argv."""
    cmd = get_command(app)
    # Show help to avoid requiring a command; prevent pytest argv from breaking
    cmd.main(args=["--help"], prog_name="mcp-agent-mail", standalone_mode=False)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
