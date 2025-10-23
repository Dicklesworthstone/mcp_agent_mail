"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from .app import build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .http import build_http_app
from .models import Agent, Project

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
    app = build_http_app(settings, server)
    uvicorn.run(app, host=resolved_host, port=resolved_port, log_level="info")


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


@app.command("migrate")
def migrate() -> None:
    """Ensure database schema and FTS structures exist."""
    settings = get_settings()
    asyncio.run(ensure_schema(settings))
    console.print("[green]Database migrations complete.[/]")


@app.command("list-projects")
def list_projects(include_agents: bool = typer.Option(False, help="Include agent counts.")) -> None:
    """List known projects."""

    settings = get_settings()

    async def _collect() -> list[tuple[Project, int]]:
        await ensure_schema(settings)
        async with get_session() as session:
            result = await session.execute(select(Project))
            projects = result.scalars().all()
            rows: list[tuple[Project, int]] = []
            if include_agents:
                for project in projects:
                    count_result = await session.execute(
                        select(func.count(Agent.id)).where(Agent.project_id == project.id)
                    )
                    count = int(count_result.scalar_one())
                    rows.append((project, count))
            else:
                rows = [(project, 0) for project in projects]
            return rows

    rows = asyncio.run(_collect())
    table = Table(title="Projects", show_lines=False)
    table.add_column("ID")
    table.add_column("Slug")
    table.add_column("Human Key")
    table.add_column("Created")
    if include_agents:
        table.add_column("Agents")
    for project, agent_count in rows:
        row = [str(project.id), project.slug, project.human_key, project.created_at.isoformat()]
        if include_agents:
            row.append(str(agent_count))
        table.add_row(*row)
    console.print(table)


if __name__ == "__main__":
    app()
