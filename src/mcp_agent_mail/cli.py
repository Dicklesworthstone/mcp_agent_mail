"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import warnings
import webbrowser
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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
from .share import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_THRESHOLD,
    DETACH_ATTACHMENT_THRESHOLD,
    INLINE_ATTACHMENT_THRESHOLD,
    SCRUB_PRESETS,
    ShareExportError,
    apply_project_scope,
    bundle_attachments,
    copy_viewer_assets,
    create_sqlite_snapshot,
    detect_hosting_hints,
    encrypt_bundle,
    export_viewer_data,
    maybe_chunk_database,
    package_directory_as_zip,
    prepare_output_directory,
    resolve_sqlite_database_path,
    scrub_snapshot,
    sign_manifest,
    write_bundle_scaffolding,
)
from .utils import slugify

# Suppress annoying bleach CSS sanitizer warning from dependencies
warnings.filterwarnings("ignore", category=UserWarning, module="bleach")

console = Console()
DEFAULT_ENV_PATH = Path(".env")
app = typer.Typer(help="Developer utilities for the MCP Agent Mail service.")

guard_app = typer.Typer(help="Install or remove the Git pre-commit guard")
file_reservations_app = typer.Typer(help="Inspect advisory file_reservations")
acks_app = typer.Typer(help="Review acknowledgement status")
share_app = typer.Typer(help="Export MCP Agent Mail data for static sharing")
config_app = typer.Typer(help="Configure server settings")

app.add_typer(guard_app, name="guard")
app.add_typer(file_reservations_app, name="file_reservations")
app.add_typer(acks_app, name="acks")
app.add_typer(share_app, name="share")
app.add_typer(config_app, name="config")


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


@share_app.command("export")
def share_export(
    output: Annotated[str, typer.Option("--output", "-o", help="Directory where the static bundle should be written.")],
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive",
            "-i",
            help="Launch an interactive wizard (future enhancement; currently prints guidance).",
        ),
    ] = False,
    projects: Annotated[list[str] | None, typer.Option("--project", "-p", help="Limit export to specific project slugs or human keys.")] = None,
    inline_threshold: Annotated[
        int,
        typer.Option(
            "--inline-threshold",
            help="Inline attachments ≤ this many bytes as data URIs.",
            min=0,
            show_default=True,
        ),
    ] = INLINE_ATTACHMENT_THRESHOLD,
    detach_threshold: Annotated[
        int,
        typer.Option(
            "--detach-threshold",
            help="Mark attachments ≥ this many bytes as external (not bundled).",
            min=0,
            show_default=True,
        ),
    ] = DETACH_ATTACHMENT_THRESHOLD,
    scrub_preset: Annotated[
        str,
        typer.Option(
            "--scrub-preset",
            help="Redaction preset to apply (e.g., standard, strict).",
            case_sensitive=False,
            show_default=True,
        ),
    ] = "standard",
    chunk_threshold: Annotated[
        int,
        typer.Option(
            "--chunk-threshold",
            help="Chunk the SQLite database when it exceeds this size (bytes).",
            min=0,
            show_default=True,
        ),
    ] = DEFAULT_CHUNK_THRESHOLD,
    chunk_size: Annotated[
        int,
        typer.Option(
            "--chunk-size",
            help="Chunk size in bytes when chunking is enabled.",
            min=1024,
            show_default=True,
        ),
    ] = DEFAULT_CHUNK_SIZE,
    zip_bundle: Annotated[
        bool,
        typer.Option(
            "--zip/--no-zip",
            help="Package the exported directory into a ZIP archive (enabled by default).",
            show_default=True,
        ),
    ] = True,
    signing_key: Annotated[Optional[Path], typer.Option("--signing-key", help="Path to Ed25519 signing key (32-byte seed).")]=None,
    signing_public_out: Annotated[Optional[Path], typer.Option("--signing-public-out", help="Write public key to this file after signing.")]=None,
    age_recipients: Annotated[
        tuple[str, ...] | None,
        typer.Option(
            None,
            "--age-recipient",
            help="Encrypt ZIP with age using the provided recipient(s).",
            multiple=True,
        ),  # type: ignore[arg-type]
    ] = None,
) -> None:
    """Export the MCP Agent Mail mailbox into a shareable static bundle (snapshot + scaffolding prototype)."""

    age_recipient_list = list(age_recipients or ())
    if projects is None:
        projects = []
    scrub_preset = (scrub_preset or "standard").strip().lower()
    if scrub_preset not in SCRUB_PRESETS:
        console.print(
            "[red]Invalid scrub preset:[/] "
            f"{scrub_preset}. Choose one of: {', '.join(SCRUB_PRESETS)}."
        )
        raise typer.Exit(code=1)
    raw_output = _resolve_path(output)
    try:
        output_path = prepare_output_directory(raw_output)
    except ShareExportError as exc:
        console.print(f"[red]Invalid output directory:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.rule("[bold]Static Mailbox Export[/bold]")

    try:
        database_path = resolve_sqlite_database_path()
    except ShareExportError as exc:
        console.print(f"[red]Failed to resolve SQLite database: {exc}[/]")
        raise typer.Exit(code=1) from exc

    if interactive:
        wizard = _run_share_export_wizard(
            database_path,
            inline_threshold,
            detach_threshold,
            chunk_threshold,
            chunk_size,
            scrub_preset,
        )
        projects = wizard["projects"]
        inline_threshold = wizard["inline_threshold"]
        detach_threshold = wizard["detach_threshold"]
        chunk_threshold = wizard["chunk_threshold"]
        chunk_size = wizard["chunk_size"]
        zip_bundle = wizard["zip_bundle"]
        scrub_preset = wizard["scrub_preset"]

    console.print(f"[cyan]Using database:[/] {database_path}")

    snapshot_path = output_path / "mailbox.sqlite3"
    console.print(f"[cyan]Creating snapshot:[/] {snapshot_path}")

    try:
        create_sqlite_snapshot(database_path, snapshot_path)
    except ShareExportError as exc:
        console.print(f"[red]Snapshot failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if detach_threshold <= inline_threshold:
        console.print(
            "[yellow]Adjusting detach threshold to exceed inline threshold to avoid conflicts.[/]"
        )
        detach_threshold = inline_threshold + max(1024, inline_threshold // 2 or 1)

    hosting_hints = detect_hosting_hints(output_path)
    if hosting_hints:
        table = Table(title="Detected Hosting Targets")
        table.add_column("Host")
        table.add_column("Signals")
        for hint in hosting_hints:
            table.add_row(hint.title, "\n".join(hint.signals))
        console.print(table)
    else:
        console.print("[dim]No hosting targets detected automatically; consult HOW_TO_DEPLOY.md for guidance.[/]")

    console.print("[cyan]Applying project filters and scrubbing data...[/]")
    try:
        scope = apply_project_scope(snapshot_path, projects)
    except ShareExportError as exc:
        console.print(f"[red]Project filtering failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        scrub_summary = scrub_snapshot(snapshot_path, preset=scrub_preset)
    except ShareExportError as exc:
        console.print(f"[red]Snapshot scrubbing failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    settings = get_settings()
    storage_root = Path(settings.storage.root).expanduser()
    try:
        attachments_manifest = bundle_attachments(
            snapshot_path,
            output_path,
            storage_root=storage_root,
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
        )
    except ShareExportError as exc:
        console.print(f"[red]Attachment packaging failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    chunk_manifest = maybe_chunk_database(
        snapshot_path,
        output_path,
        threshold_bytes=chunk_threshold,
        chunk_bytes=chunk_size,
    )
    if chunk_manifest:
        console.print(
            f"[cyan]Chunked database into {chunk_manifest['chunk_count']} files of ~{chunk_manifest['chunk_size']//1024} KiB.[/]"
        )

    copy_viewer_assets(output_path)
    viewer_data = export_viewer_data(snapshot_path, output_path)

    console.print("[cyan]Writing manifest and helper docs...[/]")
    try:
        write_bundle_scaffolding(
            output_path,
            snapshot=snapshot_path,
            scope=scope,
            project_filters=projects,
            scrub_summary=scrub_summary,
            attachments_manifest=attachments_manifest,
            chunk_manifest=chunk_manifest,
            hosting_hints=hosting_hints,
            viewer_data=viewer_data,
        )
    except ShareExportError as exc:
        console.print(f"[red]Failed to scaffold bundle:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if signing_key is not None:
        try:
            public_out_path = _resolve_path(signing_public_out) if signing_public_out else None
            signature_info = sign_manifest(
                output_path / "manifest.json",
                signing_key,
                output_path,
                public_out=public_out_path,
            )
            console.print(
                f"[green]✓ Signed manifest (Ed25519, public key {signature_info['public_key']})[/]"
            )
        except ShareExportError as exc:
            console.print(f"[red]Manifest signing failed:[/] {exc}")
            raise typer.Exit(code=1) from exc

    console.print("[green]✓ Created SQLite snapshot for sharing.[/]")
    console.print(
        f"[green]✓ Applied '{scrub_summary.preset}' scrub (pseudonymized {scrub_summary.agents_pseudonymized}/{scrub_summary.agents_total} agents, "
        f"{scrub_summary.secrets_replaced} secret tokens redacted, {scrub_summary.bodies_redacted} bodies replaced).[/]"
    )
    included_projects = ", ".join(record.slug for record in scope.projects)
    console.print(f"[green]✓ Project scope includes: {included_projects or 'none'}[/]")
    att_stats = attachments_manifest.get("stats", {})
    console.print(
        "[green]✓ Packaged attachments: "
        f"{att_stats.get('inline', 0)} inline, "
        f"{att_stats.get('copied', 0)} copied, "
        f"{att_stats.get('externalized', 0)} external, "
        f"{att_stats.get('missing', 0)} missing "
        f"(inline ≤ {inline_threshold} B, external ≥ {detach_threshold} B).[/]"
    )
    console.print("[green]✓ Generated manifest, README.txt, HOW_TO_DEPLOY.md, and viewer assets.[/]")

    if zip_bundle:
        archive_path = output_path.parent / f"{output_path.name}.zip"
        console.print(f"[cyan]Packaging archive:[/] {archive_path}")
        try:
            package_directory_as_zip(output_path, archive_path)
        except ShareExportError as exc:
            console.print(f"[red]Failed to create ZIP archive:[/] {exc}")
            raise typer.Exit(code=1) from exc
        console.print("[green]✓ Packaged ZIP archive for distribution.[/]")
        if age_recipient_list:
            try:
                encrypted_path = encrypt_bundle(archive_path, age_recipient_list)
                if encrypted_path:
                    console.print(f"[green]✓ Encrypted bundle written to {encrypted_path}[/]")
            except ShareExportError as exc:
                console.print(f"[red]Bundle encryption failed:[/] {exc}")
                raise typer.Exit(code=1) from exc

    console.print(
        "[dim]Next steps: flesh out the static SPA (search, thread detail) and tighten signing/encryption defaults per the roadmap.[/]"
    )


def _list_projects_for_wizard(database_path: Path) -> list[tuple[str, str]]:
    projects: list[tuple[str, str]] = []
    try:
        with sqlite3.connect(str(database_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT slug, human_key FROM projects ORDER BY slug COLLATE NOCASE").fetchall()
            for row in rows:
                slug = row["slug"] or ""
                human_key = row["human_key"] or ""
                projects.append((slug, human_key))
    except sqlite3.Error:
        pass
    return projects


def _parse_positive_int(value: str, default: int) -> int:
    text = value.strip()
    if not text:
        return default
    try:
        result = int(text)
        if result < 0:
            raise ValueError
        return result
    except ValueError:
        console.print(f"[yellow]Invalid number '{value}'. Using default {default}.[/]")
        return default


def _run_share_export_wizard(
    database_path: Path,
    default_inline: int,
    default_detach: int,
    default_chunk_threshold: int,
    default_chunk_size: int,
) -> dict[str, Any]:
    console.rule("[bold]Share Export Wizard[/bold]")
    projects = _list_projects_for_wizard(database_path)
    if projects:
        console.print("[cyan]Available projects:[/]")
        for slug, human_key in projects:
            console.print(f"  • [bold]{slug}[/] ({human_key})")
    else:
        console.print("[yellow]No projects detected in the database (exporting all projects).[/]")

    project_input = typer.prompt(
        "Enter project slugs or human keys to include (comma separated, leave blank for all)",
        default="",
    )
    selected_projects = [part.strip() for part in project_input.split(",") if part.strip()]

    inline_input = typer.prompt(
        f"Inline attachments threshold in bytes (default {default_inline})",
        default=str(default_inline),
    )
    inline_threshold = _parse_positive_int(inline_input, default_inline)

    detach_input = typer.prompt(
        f"External attachment threshold in bytes (default {default_detach})",
        default=str(default_detach),
    )
    detach_threshold = _parse_positive_int(detach_input, default_detach)

    chunk_threshold_input = typer.prompt(
        f"Chunk database when size exceeds (bytes, default {default_chunk_threshold})",
        default=str(default_chunk_threshold),
    )
    chunk_threshold = _parse_positive_int(chunk_threshold_input, default_chunk_threshold)

    chunk_size_input = typer.prompt(
        f"Chunk size in bytes (default {default_chunk_size})",
        default=str(default_chunk_size),
    )
    chunk_size = _parse_positive_int(chunk_size_input, default_chunk_size)

    zip_bundle = typer.confirm("Package the output directory as a .zip archive?", default=True)

    return {
        "projects": selected_projects,
        "inline_threshold": inline_threshold,
        "detach_threshold": detach_threshold,
        "chunk_threshold": chunk_threshold,
        "chunk_size": chunk_size,
        "scrub_preset": preset_value,
        "zip_bundle": zip_bundle,
    }


def _collect_preview_status(bundle_path: Path) -> dict[str, Any]:
    bundle_path = bundle_path.resolve()
    entries: list[str] = []
    latest_ns = 0
    manifest_ns = None
    if bundle_path.is_dir():
        for path in bundle_path.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            rel = path.relative_to(bundle_path).as_posix()
            entries.append(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}")
            latest_ns = max(latest_ns, stat.st_mtime_ns)
            if rel == "manifest.json":
                manifest_ns = stat.st_mtime_ns
    digest_input = "|".join(entries).encode("utf-8")
    signature = hashlib.sha256(digest_input).hexdigest() if entries else "0"
    payload: dict[str, Any] = {
        "signature": signature,
        "files_indexed": len(entries),
        "last_modified_ns": latest_ns or None,
    }
    if latest_ns:
        payload["last_modified_iso"] = datetime.fromtimestamp(latest_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    if manifest_ns:
        payload["manifest_ns"] = manifest_ns
        payload["manifest_iso"] = datetime.fromtimestamp(manifest_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    return payload


def _start_preview_server(bundle_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    bundle_path = bundle_path.resolve()

    class PreviewRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(bundle_path), **kwargs)

        def end_headers(self) -> None:  # type: ignore[override]
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            super().end_headers()

        def do_GET(self) -> None:  # type: ignore[override]
            if self.path.startswith("/__preview__/status"):
                payload = _collect_preview_status(bundle_path)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return super().do_GET()

    server = ThreadingHTTPServer((host, port), PreviewRequestHandler)
    server.daemon_threads = True
    return server


@share_app.command("preview")
def share_preview(
    bundle: Annotated[str, typer.Argument(help="Path to the exported bundle directory.")],
    host: Annotated[str, typer.Option("--host", help="Host interface for the preview server.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port for the preview server.")] = 9000,
    open_browser: Annotated[
        bool,
        typer.Option("--open-browser/--no-open-browser", help="Automatically open the bundle in a browser."),
    ] = False,
) -> None:
    """Serve a static export bundle locally for inspection."""

    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists() or not bundle_path.is_dir():
        console.print(f"[red]Bundle directory not found:[/] {bundle_path}")
        raise typer.Exit(code=1)

    server = _start_preview_server(bundle_path, host, port)
    actual_host, actual_port = server.server_address[:2]
    actual_host = actual_host or host

    console.rule("[bold]Static Bundle Preview[/bold]")
    console.print(f"Serving {bundle_path} at http://{actual_host}:{actual_port}/ (Ctrl+C to stop)")

    if open_browser:
        with suppress(Exception):
            webbrowser.open(f"http://{actual_host}:{actual_port}/")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down preview server...[/]")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        console.print("[green]Preview server stopped.[/]")


def _resolve_path(raw_path: str | Path) -> Path:
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


@config_app.command("set-port")
def config_set_port(
    port: int = typer.Argument(..., help="HTTP server port number"),
    env_file: Annotated[Optional[Path], typer.Option("--env-file", help="Path to .env file")] = None,
) -> None:
    """Set HTTP_PORT in .env file."""
    import re

    if port < 1 or port > 65535:
        console.print(f"[red]Error:[/red] Port must be between 1 and 65535 (got: {port})")
        raise typer.Exit(code=1)

    env_target = env_file if env_file is not None else DEFAULT_ENV_PATH
    env_path = _resolve_path(str(env_target))

    # Ensure parent directory exists
    env_path.parent.mkdir(parents=True, exist_ok=True)

    # Use atomic write pattern: write to temp file, then move
    try:
        if env_path.exists():
            # Read existing content
            content = env_path.read_text(encoding="utf-8")

            if re.search(r"^HTTP_PORT=", content, re.MULTILINE):
                # Replace existing
                new_content = re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={port}", content, flags=re.MULTILINE)
                action = "Updated"
            else:
                # Append (ensure file ends with newline first)
                if content and not content.endswith("\n"):
                    new_content = content + f"\nHTTP_PORT={port}\n"
                else:
                    new_content = content + f"HTTP_PORT={port}\n"
                action = "Added"
        else:
            # Create new file
            new_content = f"HTTP_PORT={port}\n"
            action = "Created"

        # Write to temporary file in same directory (for atomic move)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=env_path.parent, prefix=".env.tmp.", text=True
        )
        try:
            # Write content with secure permissions from the start
            # (best-effort on Windows where Unix permissions don't apply)
            with suppress(OSError, NotImplementedError):
                Path(temp_path).chmod(0o600)

            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(new_content)

            # Atomic move
            Path(temp_path).replace(env_path)

            # Ensure final permissions are secure (best-effort on Windows)
            with suppress(OSError, NotImplementedError):
                env_path.chmod(0o600)

            console.print(f"[green]✓[/green] {action} HTTP_PORT={port} in {env_path}")
        except (OSError, IOError) as inner_e:
            # Clean up temp file on error
            Path(temp_path).unlink(missing_ok=True)
            raise OSError(f"Failed to write temporary file: {inner_e}") from inner_e

    except PermissionError as e:
        console.print(f"[red]Error:[/red] Permission denied writing to {env_path}")
        raise typer.Exit(code=1) from e
    except OSError as e:
        console.print(f"[red]Error:[/red] Failed to write {env_path}: {e}")
        raise typer.Exit(code=1) from e

    console.print("\n[dim]Note: Restart the server for changes to take effect[/dim]")


@config_app.command("show-port")
def config_show_port() -> None:
    """Display the configured HTTP port."""
    settings = get_settings()
    console.print("[cyan]HTTP Server Configuration:[/cyan]")
    console.print(f"  Host: {settings.http.host}")
    console.print(f"  Port: [bold]{settings.http.port}[/bold]")
    console.print(f"  Path: {settings.http.path}")
    console.print(f"\n[dim]Full URL: http://{settings.http.host}:{settings.http.port}{settings.http.path}[/dim]")


if __name__ == "__main__":
    app()
