"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional, cast

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from sqlalchemy import asc, desc, func, select
from sqlalchemy.engine import make_url

from .app import build_mcp_server
from .config import get_settings
from .db import ensure_schema, get_session
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .http import build_http_app
from .models import Agent, FileReservation, Message, MessageRecipient, Project
from .utils import slugify

# Suppress annoying bleach CSS sanitizer warning from dependencies
warnings.filterwarnings("ignore", category=UserWarning, module="bleach")

console = Console()
app = typer.Typer(help="Developer utilities for the MCP Agent Mail service.")

guard_app = typer.Typer(help="Install or remove the Git pre-commit guard")
file_reservations_app = typer.Typer(help="Inspect advisory file_reservations")
acks_app = typer.Typer(help="Review acknowledgement status")

app.add_typer(guard_app, name="guard")
app.add_typer(file_reservations_app, name="file_reservations")
app.add_typer(acks_app, name="acks")


async def _get_project_record(identifier: str) -> Project:
    slug = slugify(identifier)
    await ensure_schema()
    async with get_session() as session:
        stmt = select(Project).where((Project.slug == slug) | (Project.human_key == identifier))
        result = await session.execute(stmt)
        project = result.scalars().first()
        if not project:
            raise ValueError(f"Project '{identifier}' not found")
        return project


async def _get_agent_record(project: Project, agent_name: str) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, Agent.name == agent_name)
        )
        agent = result.scalars().first()
        if not agent:
            raise ValueError(f"Agent '{agent_name}' not registered for project '{project.human_key}'")
        return agent


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


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

    # Display awesome startup banner with database stats
    from . import rich_logger
    rich_logger.display_startup_banner(settings, resolved_host, resolved_port, resolved_path)

    server = build_mcp_server()
    app = build_http_app(settings, server)
    # Disable WebSockets: HTTP-only MCP transport. Stay compatible with tests that
    # monkeypatch uvicorn.run without the 'ws' parameter.
    import inspect as _inspect
    _sig = _inspect.signature(uvicorn.run)
    _kwargs: dict[str, Any] = {"host": resolved_host, "port": resolved_port, "log_level": "info"}
    if "ws" in _sig.parameters:
        _kwargs["ws"] = "none"
    uvicorn.run(app, **_kwargs)


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


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    return path


@app.command("clear-and-reset-everything")
def clear_and_reset_everything(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
) -> None:
    """
    Delete the SQLite database (including WAL/SHM) and wipe all storage-root contents.
    """
    settings = get_settings()
    db_url = settings.database.url

    database_files: list[Path] = []
    try:
        url = make_url(db_url)
        if url.get_backend_name().startswith("sqlite"):
            database = url.database or ""
            if not database:
                console.print("[yellow]Warning:[/] SQLite database path is empty; nothing to delete.")
            else:
                db_path = _resolve_path(database)
                database_files.append(db_path)
                database_files.append(Path(f"{db_path}-wal"))
                database_files.append(Path(f"{db_path}-shm"))
    except Exception as exc:  # pragma: no cover - defensive
        console.print(f"[red]Failed to parse database URL '{db_url}': {exc}[/]")

    storage_root = _resolve_path(settings.storage.root)

    if not force:
        console.print("[bold yellow]This will irreversibly delete:[/]")
        if database_files:
            for path in database_files:
                console.print(f"  • {path}")
        else:
            console.print("  • (no SQLite files detected)")
        console.print(f"  • All contents inside {storage_root} (including .git)")
        console.print()
        if not typer.confirm("Proceed?"):
            raise typer.Exit(code=1)

    # Remove database files
    deleted_db_files: list[Path] = []
    for path in database_files:
        try:
            if path.exists():
                path.unlink()
                deleted_db_files.append(path)
        except Exception as exc:  # pragma: no cover - filesystem failures
            console.print(f"[red]Failed to delete {path}: {exc}[/]")

    # Wipe storage root contents completely (including .git directory)
    deleted_storage: list[Path] = []
    if storage_root.exists():
        for child in storage_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                deleted_storage.append(child)
            except Exception as exc:  # pragma: no cover
                console.print(f"[red]Failed to remove {child}: {exc}[/]")
    else:
        console.print(f"[yellow]Storage root {storage_root} does not exist; nothing to remove.[/]")

    console.print("[green]✓ Reset complete.[/]")
    if deleted_db_files:
        console.print(f"[dim]Removed database files:[/] {', '.join(str(p) for p in deleted_db_files)}")
    if deleted_storage:
        console.print(f"[dim]Cleared storage root entries:[/] {', '.join(str(p.name) for p in deleted_storage)}")


@app.command("migrate")
def migrate() -> None:
    """Create database schema from SQLModel definitions (pure SQLModel approach)."""
    settings = get_settings()
    with console.status("Creating database schema from models..."):
        # Pure SQLModel: models define schema, create_all() creates tables
        asyncio.run(ensure_schema(settings))
    console.print("[green]✓ Database schema created from model definitions![/]")
    console.print("[dim]Note: To apply model changes, delete storage.sqlite3 and run this again.[/]")


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


@guard_app.command("install")
def guard_install(
    project: str,
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
) -> None:
    """Install the advisory pre-commit guard into the given repository."""

    settings = get_settings()
    repo_path = repo.expanduser().resolve()

    async def _run() -> tuple[Project, Path]:
        project_record = await _get_project_record(project)
        hook_path = await install_guard_script(settings, project_record.slug, repo_path)
        return project_record, hook_path

    try:
        project_record, hook_path = asyncio.run(_run())
    except ValueError as exc:  # convert to CLI-friendly error
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]Installed guard for [bold]{project_record.human_key}[/] at {hook_path}.")


@guard_app.command("uninstall")
def guard_uninstall(
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
) -> None:
    """Remove the advisory pre-commit guard from the repository."""

    repo_path = repo.expanduser().resolve()
    removed = asyncio.run(uninstall_guard_script(repo_path))
    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    if removed:
        console.print(f"[green]Removed guard at {hook_path}.")
    else:
        console.print(f"[yellow]No guard found at {hook_path}.")


@file_reservations_app.command("list")
def file_reservations_list(
    project: str = typer.Argument(..., help="Project slug or human key"),
    active_only: bool = typer.Option(True, help="Show only active file_reservations"),
) -> None:
    """Display advisory file_reservations for a project."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError("Project must have an id")
        await ensure_schema()
        async with get_session() as session:
            stmt = select(FileReservation, Agent.name).join(Agent, FileReservation.agent_id == Agent.id).where(
                FileReservation.project_id == project_record.id
            )
            if active_only:
                stmt = stmt.where(cast(Any, FileReservation.released_ts).is_(None))
            stmt = stmt.order_by(asc(FileReservation.expires_ts))
            rows = (await session.execute(stmt)).all()
        return project_record, rows

    try:
        project_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"File Reservations for {project_record.human_key}", show_lines=False)
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("Released")
    for file_reservation, agent_name in rows:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _iso(file_reservation.released_ts) if file_reservation.released_ts else "",
        )
    console.print(table)


@file_reservations_app.command("active")
def file_reservations_active(
    project: str = typer.Argument(..., help="Project slug or human key"),
    limit: int = typer.Option(100, help="Max file_reservations to display"),
) -> None:
    """List active file_reservations with expiry countdowns."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError("Project must have an id")
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(FileReservation, Agent.name)
                .join(Agent, FileReservation.agent_id == Agent.id)
                .where(FileReservation.project_id == project_record.id, cast(Any, FileReservation.released_ts).is_(None))
                .order_by(asc(FileReservation.expires_ts))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return project_record, rows

    try:
        project_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)

    def _fmt_delta(dt: datetime) -> str:
        delta = dt - now
        total = int(delta.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"

    table = Table(title=f"Active File Reservations — {project_record.human_key}")
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("In")

    for file_reservation, agent_name in rows:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _fmt_delta(file_reservation.expires_ts),
        )
    console.print(table)


@file_reservations_app.command("soon")
def file_reservations_soon(
    project: str = typer.Argument(..., help="Project slug or human key"),
    minutes: int = typer.Option(30, min=1, help="Show file_reservations expiring within N minutes"),
) -> None:
    """Show file_reservations expiring soon to prompt renewals or coordination."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError("Project must have an id")
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(FileReservation, Agent.name)
                .join(Agent, FileReservation.agent_id == Agent.id)
                .where(
                    FileReservation.project_id == project_record.id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .order_by(asc(FileReservation.expires_ts))
            )
            rows = (await session.execute(stmt)).all()
        return project_record, rows

    try:
        project_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=minutes)
    soon = [(c, a) for (c, a) in rows if c.expires_ts <= cutoff]

    table = Table(title=f"File Reservations expiring within {minutes}m — {project_record.human_key}", show_lines=False)
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("In")

    def _fmt_delta(dt: datetime) -> str:
        delta = dt - now
        total = int(delta.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"

    for file_reservation, agent_name in soon:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _fmt_delta(file_reservation.expires_ts),
        )
    console.print(table)

@acks_app.command("pending")
def acks_pending(
    project: str = typer.Argument(..., help="Project slug or human key"),
    agent: str = typer.Argument(..., help="Agent name"),
    limit: int = typer.Option(20, help="Max messages to display"),
) -> None:
    """List messages that require acknowledgement and are still pending."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, Any, Any, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError("Project and agent must have IDs")
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(Message, MessageRecipient.read_ts, MessageRecipient.ack_ts, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_record.id,
                    MessageRecipient.agent_id == agent_record.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(desc(Message.created_ts))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return project_record, agent_record, rows

    try:
        project_record, agent_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"Pending ACKs for {agent_record.name} ({project_record.human_key})", show_lines=False)
    table.add_column("Msg ID")
    table.add_column("Thread")
    table.add_column("Subject")
    table.add_column("Kind")
    table.add_column("Created")
    table.add_column("Read")
    table.add_column("Ack Age")

    now = datetime.now(timezone.utc)
    def _age(dt: datetime) -> str:
        # Coerce naive datetimes from SQLite to UTC for arithmetic
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for message, read_ts, _ack_ts, kind in rows:
        age = _age(message.created_ts)
        table.add_row(
            str(message.id),
            message.thread_id or "",
            message.subject,
            kind,
            _iso(message.created_ts),
            _iso(read_ts) if read_ts else "",
            age,
        )
    console.print(table)


@acks_app.command("remind")
def acks_remind(
    project: str = typer.Argument(..., help="Project slug or human key"),
    agent: str = typer.Argument(..., help="Agent name"),
    min_age_minutes: int = typer.Option(30, help="Only show ACK-required older than N minutes"),
    limit: int = typer.Option(50, help="Max messages to display"),
) -> None:
    """Highlight pending acknowledgements older than a threshold."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, Any, Any, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError("Project and agent must have IDs")
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(Message, MessageRecipient.read_ts, MessageRecipient.ack_ts, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_record.id,
                    MessageRecipient.agent_id == agent_record.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(Message.created_ts))  # oldest first
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return project_record, agent_record, rows

    try:
        _project_record, agent_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=min_age_minutes)
    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    stale = [(m, rts, ats, k) for (m, rts, ats, k) in rows if _aware(m.created_ts) <= cutoff]

    table = Table(title=f"ACK Reminders (>{min_age_minutes}m) for {agent_record.name}")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Created")
    table.add_column("Age")
    table.add_column("Kind")
    table.add_column("Read?")

    def _age(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for msg, read_ts, _ack_ts, kind in stale:
        table.add_row(
            str(msg.id),
            msg.subject,
            _iso(msg.created_ts),
            _age(msg.created_ts),
            kind,
            "yes" if read_ts else "no",
        )
    if not stale:
        console.print("[green]No pending acknowledgements exceed the threshold.[/]")
    else:
        console.print(table)


@acks_app.command("overdue")
def acks_overdue(
    project: str = typer.Argument(..., help="Project slug or human key"),
    agent: str = typer.Argument(..., help="Agent name"),
    ttl_minutes: int = typer.Option(60, min=1, help="Only show ACK-required older than N minutes"),
    limit: int = typer.Option(50, help="Max messages to display"),
) -> None:
    """List ack-required messages older than a threshold without acknowledgements."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError("Project and agent must have IDs")
        await ensure_schema()
        async with get_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
            stmt = (
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_record.id,
                    MessageRecipient.agent_id == agent_record.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                    Message.created_ts <= cutoff,
                )
                .order_by(asc(Message.created_ts))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return project_record, agent_record, rows

    try:
        project_record, agent_record, rows = asyncio.run(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"ACK Overdue (>{ttl_minutes}m) for {agent_record.name} ({project_record.human_key})")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Created")
    table.add_column("Age")
    table.add_column("Kind")

    now = datetime.now(timezone.utc)
    def _age(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for msg, kind in rows:
        table.add_row(
            str(msg.id),
            msg.subject,
            _iso(msg.created_ts),
            _age(msg.created_ts),
            kind,
        )
    if not rows:
        console.print("[green]No overdue acknowledgements exceed the threshold.[/]")
    else:
        console.print(table)





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
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
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
