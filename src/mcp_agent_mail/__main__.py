"""Entry point for ``python -m mcp_agent_mail``.

Dispatches directly to the Typer app defined in ``cli.py`` so that
``python -m mcp_agent_mail <subcommand>`` behaves the same as
``python -m mcp_agent_mail.cli <subcommand>``.
"""

from .cli import app


def main() -> None:
    """Invoke the Typer CLI with the process argv."""
    app()


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
