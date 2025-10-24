"""Allow `python -m mcp_agent_mail` to invoke the CLI entry-point safely under pytest."""

from typer.main import get_command

from .cli import app


def main() -> None:
    """Dispatch to the Typer CLI entry-point with sanitized argv.

    Avoid consuming pytest's own options by forcing help display only.
    """
    cmd = get_command(app)
    try:
        cmd.main(args=["--help"], prog_name="mcp-agent-mail")
    except SystemExit:
        # Help prints then exits; suppress for embedding
        return


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
