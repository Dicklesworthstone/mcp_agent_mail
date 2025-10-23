"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import subprocess
from typing import Optional

import typer
from rich.console import Console

from .app import build_mcp_server
from .config import Settings, get_settings

console = Console()
app = typer.Typer(help="Developer utilities for the MCP Agent Mail service.")


@app.command("serve-stdio")
def serve_stdio() -> None:
    """Run the MCP server over STDIO transport for local tooling."""
    settings: Settings = get_settings()
    console.rule("[bold blue]Starting MCP Agent Mail (STDIO)")
    console.print(f"Environment: [bold]{settings.environment}[/]")
    server = build_mcp_server()
    server.run(transport="stdio")


@app.command("serve-http")
def serve_http(
    host: Optional[str] = typer.Option(None, help="Host interface for HTTP transport. Defaults to HTTP_HOST setting."),
    port: Optional[int] = typer.Option(None, help="Port for HTTP transport. Defaults to HTTP_PORT setting."),
    path: Optional[str] = typer.Option(None, help="HTTP path where the MCP endpoint is exposed."),
) -> None:
    """Run the MCP server over the Streamable HTTP transport."""
    settings = get_settings()
    resolved_host = host or settings.http.host
    resolved_port = port or settings.http.port
    resolved_path = path or settings.http.path

    console.rule("[bold blue]Starting MCP Agent Mail (HTTP)")
    console.print(
        f"Environment: [bold]{settings.environment}[/] | "
        f"Endpoint: [green]http://{resolved_host}:{resolved_port}{resolved_path}[/]"
    )

    server = build_mcp_server()
    server.run(transport="http", host=resolved_host, port=resolved_port, path=resolved_path)


def _run_command(command: list[str]) -> None:
    console.print(f"[cyan]$ {' '.join(command)}[/]")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


@app.command("lint")
def lint() -> None:
    """Run Ruff linting with automatic fixes."""
    _run_command(["ruff", "check", "--fix", "--unsafe-fixes"])


@app.command("typecheck")
def typecheck() -> None:
    """Run MyPy type checking."""
    _run_command(["uvx", "ty", "check"])


if __name__ == "__main__":
    app()
