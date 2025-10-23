"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Optional, cast

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from sqlalchemy import asc, desc, func, select

from .app import build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .http import build_http_app
from .models import Agent, Claim, Message, MessageRecipient, Project
from .utils import slugify

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
    console.rule("[bold]Running Ruff Lint[/bold]")
    _run_command(["ruff", "check", "--fix", "--unsafe-fixes"])
    console.print("[green]Linting complete.[/]")


@app.command("typecheck")
def typecheck() -> None:
    """Run MyPy type checking."""
    console.rule("[bold]Running Type Checker[/bold]")
    _run_command(["uvx", "ty", "check"])
    console.print("[green]Type check complete.[/]")


@app.command("migrate")
def migrate() -> None:
    """Ensure database schema and FTS structures exist."""
    settings = get_settings()
    with console.status("Applying migrations..."):
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

    with console.status("Collecting project data..."):
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


@app.command("guard-install")
def guard_install(
    project_key: str = typer.Argument(..., help="Project human key or slug for the archive."),
    code_repo_path: str = typer.Argument(..., help="Path to the code repository where to install the pre-commit hook."),
) -> None:
    """Install the pre-commit guard hook into a code repository."""
    settings: Settings = get_settings()

    async def _install() -> str:
        slug = slugify(project_key)
        archive = await ensure_archive(settings, slug)
        script = await _build_precommit_hook_content(archive)
        from pathlib import Path
        import os

        repo_path = Path(code_repo_path).expanduser().resolve()
        hooks_dir = repo_path / ".git" / "hooks"
        if not hooks_dir.is_dir():
            raise typer.BadParameter(f"No git hooks directory at {hooks_dir}")
        hook_path = hooks_dir / "pre-commit"
        hook_path.write_text(script, encoding="utf-8")
        os.chmod(hook_path, 0o755)
        return str(hook_path)

    console.rule("[bold blue]Installing Pre-commit Guard")
    path = asyncio.run(_install())
    console.print(f"[green]Installed guard at[/] {path}")


@app.command("guard-uninstall")
def guard_uninstall(
    code_repo_path: str = typer.Argument(..., help="Path to the code repository to remove the pre-commit hook from."),
) -> None:
    """Remove the pre-commit guard hook if present."""
    from pathlib import Path

    repo_path = Path(code_repo_path).expanduser().resolve()
    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    console.rule("[bold blue]Uninstalling Pre-commit Guard")
    if hook_path.exists():
        hook_path.unlink()
        console.print(f"[green]Removed guard at[/] {hook_path}")
    else:
        console.print(f"[yellow]No guard found at[/] {hook_path}")


@app.command("list-acks")
def list_acks(
    project_key: str = typer.Option(..., "--project", help="Project human key or slug."),
    agent_name: str = typer.Option(..., "--agent", help="Agent name to query."),
    limit: int = typer.Option(20, help="Max messages to show."),
) -> None:
    """List messages requiring acknowledgement for an agent where ack is missing."""

    async def _collect() -> list[tuple[Message, str]]:
        await ensure_schema()
        async with get_session() as session:
            # Resolve project and agent
            proj_result = await session.execute(select(Project).where((Project.slug == slugify(project_key)) | (Project.human_key == project_key)))
            project = proj_result.scalars().first()
            if not project:
                raise typer.BadParameter(f"Project not found for key: {project_key}")
            agent_result = await session.execute(
                select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name) == agent_name.lower())
            )
            agent = agent_result.scalars().first()
            if not agent:
                raise typer.BadParameter(f"Agent '{agent_name}' not found in project '{project.human_key}'")
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project.id,
                    MessageRecipient.agent_id == agent.id,
                    Message.ack_required == True,
                    MessageRecipient.ack_ts == None,
                )
                .order_by(desc(Message.created_ts))
                .limit(limit)
            )
            return rows.all()

    console.rule("[bold blue]Ack-required Messages")
    rows = asyncio.run(_collect())
    table = Table(title=f"Pending Acks for {agent_name}")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Importance")
    table.add_column("Created")
    for msg, _ in rows:
        table.add_row(str(msg.id or ""), msg.subject, msg.importance, msg.created_ts.isoformat())
    console.print(table)


if __name__ == "__main__":
    app()
