"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, Optional, cast

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from sqlalchemy import asc, bindparam, desc, func, select, text
from sqlalchemy.engine import make_url

from .app import build_mcp_server
from .config import get_settings
from .db import ensure_schema, get_session
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .http import build_http_app
from .models import Agent, FileReservation, Message, MessageRecipient, Product, ProductProjectLink, Project
from .share import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_THRESHOLD,
    DETACH_ATTACHMENT_THRESHOLD,
    INLINE_ATTACHMENT_THRESHOLD,
    SCRUB_PRESETS,
    ShareExportError,
    build_bundle_assets,
    copy_viewer_assets,
    create_snapshot_context,
    detect_hosting_hints,
    encrypt_bundle,
    package_directory_as_zip,
    prepare_output_directory,
    resolve_sqlite_database_path,
    sign_manifest,
    summarize_snapshot,
)
from .storage import ensure_archive
from .utils import slugify

# Suppress annoying bleach CSS sanitizer warning from dependencies
warnings.filterwarnings("ignore", category=UserWarning, module="bleach")

console = Console()
DEFAULT_ENV_PATH = Path(".env")
app = typer.Typer(help="Developer utilities for the MCP Agent Mail service.")

_PREVIEW_FORCE_TOKEN = 0
_PREVIEW_FORCE_LOCK = threading.Lock()

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
mail_app = typer.Typer(help="Mail diagnostics and routing status")
app.add_typer(mail_app, name="mail")
projects_app = typer.Typer(help="Project maintenance utilities")
app.add_typer(projects_app, name="projects")
amctl_app = typer.Typer(help="Build and environment helpers")
app.add_typer(amctl_app, name="amctl")
products_app = typer.Typer(help="Product Bus: manage products and links")
app.add_typer(products_app, name="products")


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


@products_app.command("ensure")
def products_ensure(
    product_key: Annotated[Optional[str], typer.Argument(help="Product uid or name")] = None,
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Product display name")] = None,
) -> None:
    """
    Ensure a product exists (creates if missing) and print its identifiers.
    """
    key = (product_key or name or "").strip()
    if not key:
        raise typer.BadParameter("Provide a product_key or --name.")
    # Prefer server tool to ensure consistent uid policy
    settings = get_settings()
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    bearer = settings.http.bearer_token or os.environ.get("HTTP_BEARER_TOKEN", "")
    resp_data: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=5.0) as client:
            headers = {}
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            arguments: dict[str, Any] = {}
            if product_key:
                arguments["product_key"] = product_key
            if name:
                arguments["name"] = name
            req = {
                "jsonrpc": "2.0",
                "id": "cli-products-ensure",
                "method": "tools/call",
                "params": {
                    "name": "ensure_product",
                    "arguments": arguments,
                },
            }
            resp = client.post(server_url, json=req, headers=headers)
            data = resp.json()
            result = (data or {}).get("result") or {}
            if result:
                resp_data = result
    except Exception:
        resp_data = {}
    if not resp_data:
        # Fallback to local DB with the same strict uid policy
        async def _ensure_local() -> dict[str, Any]:
            await ensure_schema()
            async with get_session() as session:
                existing = await session.execute(
                    select(Product).where((Product.product_uid == key) | (Product.name == key))
                )
                prod = existing.scalars().first()
                if prod:
                    return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": prod.created_at}
                import re as _re
                import uuid as _uuid
                uid_pattern = _re.compile(r"^[A-Fa-f0-9]{8,64}$")
                if product_key and uid_pattern.fullmatch(product_key.strip()):
                    uid = product_key.strip().lower()
                else:
                    uid = _uuid.uuid4().hex[:20]
                display_name = (name or key).strip()
                display_name = " ".join(display_name.split())[:255] or uid
                prod = Product(product_uid=uid, name=display_name)
                session.add(prod)
                await session.commit()
                await session.refresh(prod)
                return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": prod.created_at}
        resp_data = asyncio.run(_ensure_local())
    table = Table(title="Product", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("id", str(resp_data.get("id", "")))
    table.add_row("product_uid", str(resp_data.get("product_uid", "")))
    table.add_row("name", str(resp_data.get("name", "")))
    _created = resp_data.get("created_at", "")
    try:
        if hasattr(_created, "isoformat"):
            _created = _created.isoformat()
    except Exception:
        pass
    table.add_row("created_at", str(_created))
    console.print(table)


@products_app.command("link")
def products_link(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    project: Annotated[str, typer.Argument(..., help="Project slug or path")],
) -> None:
    """
    Link a project into a product (idempotent).
    """
    async def _link() -> dict:
        await ensure_schema()
        prod = await _get_product_record(product_key.strip())
        proj = await _get_project_record(project)
        async with get_session() as session:
            existing = await session.execute(
                select(ProductProjectLink).where(
                    ProductProjectLink.product_id == prod.id, ProductProjectLink.project_id == proj.id
                )
            )
            link = existing.scalars().first()
            if link is None:
                link = ProductProjectLink(product_id=int(prod.id), project_id=int(proj.id))
                session.add(link)
                await session.commit()
                await session.refresh(link)
        return {"product_uid": prod.product_uid, "product_name": prod.name, "project_slug": proj.slug}
    res = asyncio.run(_link())
    console.print(f"[green]Linked[/] project '{res['project_slug']}' into product '{res['product_name']}' ({res['product_uid']}).")


@products_app.command("status")
def products_status(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
) -> None:
    """
    Show product metadata and linked projects.
    """
    async def _status() -> tuple[Product, list[Project]]:
        await ensure_schema()
        async with get_session() as session:
            stmt_prod = select(Product).where((Product.product_uid == product_key) | (Product.name == product_key))
            prod = (await session.execute(stmt_prod)).scalars().first()
            if prod is None:
                raise typer.BadParameter(f"Product '{product_key}' not found.")
            rows = await session.execute(
                select(Project).join(ProductProjectLink, ProductProjectLink.project_id == Project.id).where(
                    ProductProjectLink.product_id == prod.id
                )
            )
            projects = list(rows.scalars().all())
            return prod, projects
    prod, projects = asyncio.run(_status())
    table = Table(title=f"Product: {prod.name}", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("id", str(prod.id))
    table.add_row("product_uid", prod.product_uid)
    table.add_row("name", prod.name)
    table.add_row("created_at", _iso(prod.created_at))
    console.print(table)
    pt = Table(title="Linked Projects", show_lines=False)
    pt.add_column("id")
    pt.add_column("slug")
    pt.add_column("human_key")
    for p in projects:
        pt.add_row(str(p.id), p.slug, p.human_key)
    console.print(pt)


@products_app.command("search")
def products_search(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    query: Annotated[str, typer.Argument(..., help="FTS query")],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max results",)] = 20,
) -> None:
    """
    Full-text search over messages for all projects linked to a product.
    """
    async def _run() -> list[dict]:
        await ensure_schema()
        async with get_session() as session:
            stmt_prod = select(Product).where((Product.product_uid == product_key) | (Product.name == product_key))
            prod = (await session.execute(stmt_prod)).scalars().first()
            if prod is None:
                raise typer.BadParameter(f"Product '{product_key}' not found.")
            rows = await session.execute(
                select(ProductProjectLink.project_id).where(ProductProjectLink.product_id == prod.id)
            )
            proj_ids = [int(r[0]) for r in rows.fetchall()]
            if not proj_ids:
                return []
            result = await session.execute(
                text(
                    """
                    SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                           m.thread_id, a.name AS sender_name, m.project_id
                    FROM fts_messages
                    JOIN messages m ON fts_messages.rowid = m.id
                    JOIN agents a ON m.sender_id = a.id
                    WHERE m.project_id IN :proj_ids AND fts_messages MATCH :query
                    ORDER BY bm25(fts_messages) ASC
                    LIMIT :limit
                    """
                ).bindparams(bindparam("proj_ids", expanding=True)),
                {"proj_ids": proj_ids, "query": query, "limit": limit},
            )
            return [dict(row) for row in result.mappings().all()]
    rows = asyncio.run(_run())
    if not rows:
        console.print("[yellow]No results.[/]")
        return
    t = Table(title=f"Product search: '{query}'", show_lines=False)
    t.add_column("project_id")
    t.add_column("id")
    t.add_column("subject")
    t.add_column("from")
    t.add_column("created_ts")
    for r in rows:
        t.add_row(str(r["project_id"]), str(r["id"]), r["subject"], r["sender_name"], r["created_ts"].isoformat() if hasattr(r["created_ts"], "isoformat") else str(r["created_ts"]))
    console.print(t)


@products_app.command("inbox")
def products_inbox(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    agent: Annotated[str, typer.Argument(..., help="Agent name")],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max messages",)] = 20,
    urgent_only: Annotated[bool, typer.Option("--urgent-only/--all", help="Only high/urgent")] = False,
    include_bodies: Annotated[bool, typer.Option("--include-bodies/--no-bodies", help="Include body_md")] = False,
    since_ts: Annotated[Optional[str], typer.Option("--since-ts", help="ISO-8601 timestamp filter")] = None,
) -> None:
    """
    Fetch recent inbox messages for an agent across all projects in a product.
    Prefers server tool; falls back to local DB when server is not reachable.
    """
    settings = get_settings()
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    bearer = settings.http.bearer_token or os.environ.get("HTTP_BEARER_TOKEN", "")
    # Try server first
    try:
        with httpx.Client(timeout=5.0) as client:
            headers = {}
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            req = {
                "jsonrpc": "2.0",
                "id": "cli-products-inbox",
                "method": "tools/call",
                "params": {
                    "name": "fetch_inbox_product",
                    "arguments": {
                        "product_key": product_key,
                        "agent_name": agent,
                        "limit": int(limit),
                        "urgent_only": bool(urgent_only),
                        "include_bodies": bool(include_bodies),
                        "since_ts": since_ts or "",
                    },
                },
            }
            resp = client.post(server_url, json=req, headers=headers)
            data = resp.json()
            result = (data or {}).get("result")
            rows = result if isinstance(result, list) else []
    except Exception:
        rows = []
    if not rows:
        # Fallback: local DB
        async def _fallback() -> list[dict]:
            await ensure_schema()
            async with get_session() as session:
                prod = (await session.execute(select(Product).where((Product.product_uid == product_key) | (Product.name == product_key)))).scalars().first()
                if prod is None:
                    return []
                proj_rows = await session.execute(
                    select(Project).join(ProductProjectLink, ProductProjectLink.project_id == Project.id).where(
                        ProductProjectLink.product_id == prod.id
                    )
                )
                projects = list(proj_rows.scalars().all())
                items: list[dict] = []
                for proj in projects:
                    agent_row = (await session.execute(select(Agent).where(Agent.project_id == proj.id, Agent.name == agent))).scalars().first()
                    if not agent_row:
                        continue
                    from sqlalchemy.orm import aliased as _aliased  # local to avoid top-level churn
                    sender_alias = _aliased(Agent)
                    stmt = (
                        select(Message, MessageRecipient.kind, sender_alias.name)
                        .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                        .join(sender_alias, Message.sender_id == sender_alias.id)
                        .where(Message.project_id == proj.id, MessageRecipient.agent_id == agent_row.id)
                        .order_by(desc(Message.created_ts))
                        .limit(limit)
                    )
                    if urgent_only:
                        from typing import Any as _Any
                        stmt = stmt.where(cast(_Any, Message.importance).in_(["high", "urgent"]))
                    if since_ts:
                        try:
                            s = since_ts.strip()
                            s = s[:-1] + "+00:00" if s.endswith("Z") else s
                            from datetime import datetime as _dt
                            since_dt = _dt.fromisoformat(s)
                            stmt = stmt.where(Message.created_ts > since_dt)
                        except Exception:
                            pass
                    res = await session.execute(stmt)
                    for msg, kind, sender_name in res.all():
                        payload = {
                            "id": msg.id,
                            "project_id": proj.id,
                            "subject": msg.subject,
                            "importance": msg.importance,
                            "ack_required": msg.ack_required,
                            "created_ts": msg.created_ts,
                            "from": sender_name,
                            "kind": kind,
                        }
                        if include_bodies:
                            payload["body_md"] = msg.body_md
                        items.append(payload)
                # Sort desc by created_ts
                items.sort(key=lambda r: r.get("created_ts") or 0, reverse=True)
                return items[: max(0, int(limit))]
        rows = asyncio.run(_fallback())
    if not rows:
        console.print("[yellow]No messages found.[/]")
        return
    t = Table(title=f"Inbox for {agent} in product '{product_key}'", show_lines=False)
    t.add_column("project_id")
    t.add_column("id")
    t.add_column("subject")
    t.add_column("from")
    t.add_column("importance")
    t.add_column("created_ts")
    for r in rows:
        created = r.get("created_ts")
        if hasattr(created, "isoformat"):
            created = created.isoformat()
        t.add_row(str(r.get("project_id", "")), str(r.get("id", "")), str(r.get("subject", "")), str(r.get("from", "")), str(r.get("importance", "")), str(created or ""))
    console.print(t)


@products_app.command("summarize-thread")
def products_summarize_thread(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    thread_id: Annotated[str, typer.Argument(..., help="Thread id or key")],
    per_thread_limit: Annotated[int, typer.Option("--per-thread-limit", "-n", help="Max messages per thread",)] = 50,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Disable LLM refinement")] = False,
) -> None:
    """
    Summarize a thread across all projects in a product. Prefers server tool; minimal fallback if server is unavailable.
    """
    settings = get_settings()
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    bearer = settings.http.bearer_token or os.environ.get("HTTP_BEARER_TOKEN", "")
    # Try server
    try:
        with httpx.Client(timeout=8.0) as client:
            headers = {}
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            req = {
                "jsonrpc": "2.0",
                "id": "cli-products-summarize-thread",
                "method": "tools/call",
                "params": {
                    "name": "summarize_thread_product",
                    "arguments": {
                        "product_key": product_key,
                        "thread_id": thread_id,
                        "include_examples": True,
                        "llm_mode": (not no_llm),
                        "per_thread_limit": int(per_thread_limit),
                    },
                },
            }
            resp = client.post(server_url, json=req, headers=headers)
            data = resp.json()
            result = (data or {}).get("result") or {}
    except Exception:
        result = {}
    if not result:
        console.print("[yellow]Server unavailable; summarization requires server tool. Try again when server is running.[/]")
        raise typer.Exit(code=2)
    # Pretty print
    summary = result.get("summary") or {}
    examples = result.get("examples") or []
    table = Table(title=f"Thread summary: {thread_id}", show_lines=False)
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("participants", ", ".join(summary.get("participants", [])))
    table.add_row("total_messages", str(summary.get("total_messages", "")))
    table.add_row("open_actions", str(summary.get("open_actions", "")))
    table.add_row("done_actions", str(summary.get("done_actions", "")))
    console.print(table)
    if summary.get("key_points"):
        kp = Table(title="Key Points", show_lines=False)
        kp.add_column("point")
        for p in summary["key_points"]:
            kp.add_row(str(p))
        console.print(kp)
    if summary.get("action_items"):
        act = Table(title="Action Items", show_lines=False)
        act.add_column("item")
        for a in summary["action_items"]:
            act.add_row(str(a))
        console.print(act)
    if examples:
        ex = Table(title="Examples", show_lines=False)
        ex.add_column("id")
        ex.add_column("subject")
        ex.add_column("from")
        ex.add_column("created_ts")
        for e in examples:
            ex.add_row(str(e.get("id", "")), str(e.get("subject", "")), str(e.get("from", "")), str(e.get("created_ts", "")))
        console.print(ex)


async def _get_product_record(key: str) -> Product:
    """Fetch Product by uid or name."""
    await ensure_schema()
    async with get_session() as session:
        stmt = select(Product).where((Product.product_uid == key) | (Product.name == key))
        result = await session.execute(stmt)
        prod = result.scalars().first()
        if not prod:
            raise ValueError(f"Product '{key}' not found")
        return prod


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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Generate a security summary without writing bundle artifacts.",
            show_default=True,
        ),
    ] = False,
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
        Optional[list[str]],
        typer.Option(
            "--age-recipient",
            help="Encrypt the ZIP archive with age using the provided recipient(s). May be passed multiple times.",
        ),
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
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if dry_run:
            temp_dir = tempfile.TemporaryDirectory(prefix="mailbox-share-dry-run-")
            output_path = Path(temp_dir.name)
        else:
            output_path = prepare_output_directory(raw_output)
    except ShareExportError as exc:
        console.print(f"[red]Invalid output directory:[/] {exc}")
        if temp_dir is not None:
            temp_dir.cleanup()
        raise typer.Exit(code=1) from exc

    console.rule("[bold]Static Mailbox Export[/bold]")

    try:
        database_path = resolve_sqlite_database_path()
    except ShareExportError as exc:
        console.print(f"[red]Failed to resolve SQLite database: {exc}[/]")
        if temp_dir is not None:
            temp_dir.cleanup()
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
        snapshot_ctx = create_snapshot_context(
            source_database=database_path,
            snapshot_path=snapshot_path,
            project_filters=projects,
            scrub_preset=scrub_preset,
        )
    except ShareExportError as exc:
        console.print(f"[red]Snapshot preparation failed:[/] {exc}")
        if temp_dir is not None:
            temp_dir.cleanup()
        raise typer.Exit(code=1) from exc

    scope = snapshot_ctx.scope
    scrub_summary = snapshot_ctx.scrub_summary
    fts_enabled = snapshot_ctx.fts_enabled
    if not fts_enabled:
        console.print("[yellow]FTS5 not available; viewer will fall back to LIKE search.[/]")
    else:
        console.print("[green]✓ Built FTS5 index for snapshot search.[/]")

    settings = get_settings()
    storage_root = Path(settings.storage.root).expanduser()

    if dry_run:
        summary = summarize_snapshot(
            snapshot_path,
            storage_root=storage_root,
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
        )

        console.rule("[bold]Dry-Run Summary[/bold]")
        overview = Table(show_header=False)
        projects_text = ", ".join(p["slug"] for p in summary["projects"]) or "All projects"
        overview.add_row("Projects", projects_text)
        overview.add_row("Messages", str(summary["messages"]))
        overview.add_row("Threads", str(summary["threads"]))
        overview.add_row("FTS Search", "enabled" if fts_enabled else "fallback (LIKE)")
        attachments = summary["attachments"]
        overview.add_row(
            "Attachments",
            (
                f"total={attachments['total']} inline≤{inline_threshold}B:{attachments['inline_candidates']} "
                f"external≥{detach_threshold}B:{attachments['external_candidates']} missing:{attachments['missing']}"
            ),
        )
        overview.add_row(
            "Largest attachment",
            f"{attachments['largest_bytes']} bytes" if attachments["largest_bytes"] else "n/a",
        )
        console.print(overview)

        console.rule("Security Checklist")
        checklist = [
            f"Scrub preset: {scrub_summary.preset}",
            f"Agents pseudonymized: {scrub_summary.agents_pseudonymized}/{scrub_summary.agents_total}",
            f"Ack flags cleared: {scrub_summary.ack_flags_cleared}",
            f"Recipients read/ack cleared: {scrub_summary.recipients_cleared}",
            f"File reservations removed: {scrub_summary.file_reservations_removed}",
            f"Agent links removed: {scrub_summary.agent_links_removed}",
            f"Secrets redacted: {scrub_summary.secrets_replaced}",
            f"Bodies redacted: {scrub_summary.bodies_redacted}",
            f"Attachments cleared: {scrub_summary.attachments_cleared}",
        ]
        for item in checklist:
            console.print(f" • {item}")

        console.print()
        console.print(
            "[cyan]Run without --dry-run to generate the bundle. Consider enabling signing ( --signing-key ) and encryption (--age-recipient ) before publishing.[/]"
        )

        if temp_dir is not None:
            temp_dir.cleanup()
            temp_dir = None
        return

    export_config: dict[str, Any] = {
        "inline_threshold": inline_threshold,
        "detach_threshold": detach_threshold,
        "chunk_threshold": chunk_threshold,
        "chunk_size": chunk_size,
        "scrub_preset": scrub_preset,
        "projects": list(projects),
    }

    console.print("[cyan]Packaging attachments, viewer assets, and manifest...[/]")
    try:
        bundle_artifacts = build_bundle_assets(
            snapshot_ctx.snapshot_path,
            output_path,
            storage_root=storage_root,
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
            chunk_threshold=chunk_threshold,
            chunk_size=chunk_size,
            scope=scope,
            project_filters=projects,
            scrub_summary=scrub_summary,
            hosting_hints=hosting_hints,
            fts_enabled=fts_enabled,
            export_config=export_config,
        )
    except ShareExportError as exc:
        console.print(f"[red]Failed to build bundle assets:[/] {exc}")
        if temp_dir is not None:
            temp_dir.cleanup()
        raise typer.Exit(code=1) from exc

    attachments_manifest = bundle_artifacts.attachments_manifest
    chunk_manifest = bundle_artifacts.chunk_manifest
    if chunk_manifest:
        console.print(
            f"[cyan]Chunked database into {chunk_manifest['chunk_count']} files of ~{chunk_manifest['chunk_size']//1024} KiB.[/]"
        )


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
            if temp_dir is not None:
                temp_dir.cleanup()
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
    if fts_enabled:
        console.print("[green]✓ Built FTS5 index for full-text viewer search.[/]")
    else:
        console.print("[yellow]Search fallback active (FTS5 unavailable in current sqlite build).[/]")
    console.print("[green]✓ Generated manifest, README.md, HOW_TO_DEPLOY.md, and viewer assets.[/]")

    if zip_bundle:
        archive_path = output_path.parent / f"{output_path.name}.zip"
        console.print(f"[cyan]Packaging archive:[/] {archive_path}")
        try:
            package_directory_as_zip(output_path, archive_path)
        except ShareExportError as exc:
            console.print(f"[red]Failed to create ZIP archive:[/] {exc}")
            if temp_dir is not None:
                temp_dir.cleanup()
            raise typer.Exit(code=1) from exc
        console.print("[green]✓ Packaged ZIP archive for distribution.[/]")
        if age_recipient_list:
            try:
                encrypted_path = encrypt_bundle(archive_path, age_recipient_list)
                if encrypted_path:
                    console.print(f"[green]✓ Encrypted bundle written to {encrypted_path}[/]")
            except ShareExportError as exc:
                console.print(f"[red]Bundle encryption failed:[/] {exc}")
                if temp_dir is not None:
                    temp_dir.cleanup()
                raise typer.Exit(code=1) from exc

    if temp_dir is not None:
        temp_dir.cleanup()

    console.print(
        "[dim]Next steps: flesh out the static SPA (search, thread detail) and tighten signing/encryption defaults per the roadmap.[/]"
    )


def _list_projects_for_wizard(database_path: Path) -> list[tuple[str, str]]:
    projects: list[tuple[str, str]] = []
    conn = sqlite3.connect(str(database_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT slug, human_key FROM projects ORDER BY slug COLLATE NOCASE").fetchall()
        for row in rows:
            slug = row["slug"] or ""
            human_key = row["human_key"] or ""
            projects.append((slug, human_key))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
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
    default_scrub_preset: str,
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

    console.print("[cyan]Scrub presets:[/]")
    for name, config in SCRUB_PRESETS.items():
        console.print(f"  • [bold]{name}[/] - {config['description']}")
    preset_input = typer.prompt(
        f"Scrub preset (default {default_scrub_preset})",
        default=default_scrub_preset,
    )
    preset_value = (preset_input or default_scrub_preset).strip().lower()
    if preset_value not in SCRUB_PRESETS:
        console.print(
            f"[yellow]Unknown preset '{preset_value}'. Using {default_scrub_preset} instead.[/]"
        )
        preset_value = default_scrub_preset

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


def _bump_preview_force_token() -> int:
    global _PREVIEW_FORCE_TOKEN
    with _PREVIEW_FORCE_LOCK:
        _PREVIEW_FORCE_TOKEN = (_PREVIEW_FORCE_TOKEN + 1) % (2 ** 63)
        return _PREVIEW_FORCE_TOKEN


def _collect_preview_status(bundle_path: Path) -> dict[str, Any]:
    with _PREVIEW_FORCE_LOCK:
        token = _PREVIEW_FORCE_TOKEN
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
    entries.append(f"manual:{token}")
    digest_input = "|".join(entries).encode("utf-8")
    signature = hashlib.sha256(digest_input).hexdigest() if entries else "0"
    payload: dict[str, Any] = {
        "signature": signature,
        "files_indexed": len(entries),
        "last_modified_ns": latest_ns or None,
        "manual_token": token,
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
            # Quiet common noisy requests in preview
            if self.path == "/favicon.ico" or self.path.endswith(".map") or self.path.startswith("/.well-known/"):
                # Return 204 No Content to avoid browser/server 404 noise
                self.send_response(204)
                self.end_headers()
                return
            return super().do_GET()

    server = ThreadingHTTPServer((host, port), PreviewRequestHandler)
    server.daemon_threads = True
    return server


@share_app.command("update")
def share_update(
    bundle: Annotated[str, typer.Argument(help="Path to the existing bundle directory (e.g., your GitHub Pages repo).")],
    projects: Annotated[
        list[str] | None,
        typer.Option(
            "--project",
            "-p",
            help="Override project scope for this update (slugs or human keys). May be provided multiple times.",
        ),
    ] = None,
    inline_threshold_override: Annotated[
        Optional[int],
        typer.Option("--inline-threshold", help="Override inline attachment threshold (bytes).", min=0),
    ] = None,
    detach_threshold_override: Annotated[
        Optional[int],
        typer.Option("--detach-threshold", help="Override detach attachment threshold (bytes).", min=0),
    ] = None,
    chunk_threshold_override: Annotated[
        Optional[int],
        typer.Option("--chunk-threshold", help="Override chunking threshold (bytes).", min=0),
    ] = None,
    chunk_size_override: Annotated[
        Optional[int],
        typer.Option("--chunk-size", help="Override chunk size when chunking is enabled.", min=1024),
    ] = None,
    scrub_preset_override: Annotated[
        Optional[str],
        typer.Option(
            "--scrub-preset",
            help="Override scrub preset (standard, strict, ...).",
            case_sensitive=False,
        ),
    ] = None,
    zip_bundle: Annotated[
        bool,
        typer.Option("--zip/--no-zip", help="Package the updated bundle into a ZIP archive.", show_default=True),
    ] = False,
    signing_key: Annotated[Optional[Path], typer.Option("--signing-key", help="Path to Ed25519 signing key (32-byte seed).")]=None,
    signing_public_out: Annotated[Optional[Path], typer.Option("--signing-public-out", help="Write public key to this file after signing.")]=None,
    age_recipients: Annotated[
        Optional[list[str]],
        typer.Option(
            "--age-recipient",
            help="Encrypt the ZIP archive with age using the provided recipient(s). May be passed multiple times.",
        ),
    ] = None,
) -> None:
    """Refresh an existing static mailbox bundle using the previous export settings."""

    age_recipient_list = list(age_recipients or ())
    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists() or not bundle_path.is_dir():
        console.print(f"[red]Bundle path {bundle_path} does not exist or is not a directory.[/]")
        raise typer.Exit(code=1)

    manifest_path = bundle_path / "manifest.json"
    if not manifest_path.exists():
        console.print(f"[red]manifest.json not found inside {bundle_path}. Are you sure this is a bundle directory?[/]")
        raise typer.Exit(code=1)

    try:
        stored_config = _load_bundle_export_config(bundle_path)
    except ShareExportError as exc:
        console.print(f"[red]Failed to load existing bundle configuration:[/] {exc}")
        raise typer.Exit(code=1) from exc

    project_filters = list(projects) if projects else list(stored_config.projects)
    scrub_preset = (scrub_preset_override or stored_config.scrub_preset or "standard").strip().lower()
    if scrub_preset not in SCRUB_PRESETS:
        console.print(
            "[red]Invalid scrub preset override:[/] "
            f"{scrub_preset}. Choose one of: {', '.join(SCRUB_PRESETS)}."
        )
        raise typer.Exit(code=1)

    inline_threshold = inline_threshold_override if inline_threshold_override is not None else stored_config.inline_threshold
    detach_threshold = detach_threshold_override if detach_threshold_override is not None else stored_config.detach_threshold
    chunk_threshold = chunk_threshold_override if chunk_threshold_override is not None else stored_config.chunk_threshold
    chunk_size = chunk_size_override if chunk_size_override is not None else stored_config.chunk_size

    if inline_threshold < 0:
        console.print("[red]Inline threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if detach_threshold < 0:
        console.print("[red]Detach threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if chunk_threshold < 0:
        console.print("[red]Chunk threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if chunk_size < 1024:
        console.print("[red]Chunk size must be at least 1024 bytes.[/]")
        raise typer.Exit(code=1)

    if detach_threshold <= inline_threshold:
        console.print(
            "[yellow]Adjusting detach threshold to exceed inline threshold to avoid conflicts.[/]"
        )
        detach_threshold = inline_threshold + max(1024, inline_threshold // 2 or 1)

    existing_signature = (bundle_path / "manifest.sig.json").exists()

    console.rule("[bold]Static Mailbox Update[/bold]")

    try:
        database_path = resolve_sqlite_database_path()
    except ShareExportError as exc:
        console.print(f"[red]Failed to resolve SQLite database: {exc}[/]")
        raise typer.Exit(code=1) from exc

    console.print(f"[cyan]Using database:[/] {database_path}")

    hosting_hints = detect_hosting_hints(bundle_path)
    if hosting_hints:
        table = Table(title="Detected Hosting Targets")
        table.add_column("Host")
        table.add_column("Signals")
        for hint in hosting_hints:
            table.add_row(hint.title, "\n".join(hint.signals))
        console.print(table)
    else:
        console.print("[dim]No hosting targets detected automatically; consult HOW_TO_DEPLOY.md for guidance.[/]")

    attachments_manifest: dict[str, Any] = {}
    chunk_manifest: Optional[dict[str, Any]] = None
    scope = None
    scrub_summary = None
    fts_enabled = False

    with tempfile.TemporaryDirectory(prefix="mailbox-share-update-") as temp_dir_name:
        temp_path = Path(temp_dir_name)
        snapshot_path = temp_path / "mailbox.sqlite3"
        console.print(f"[cyan]Creating snapshot:[/] {snapshot_path}")
        try:
            snapshot_ctx = create_snapshot_context(
                source_database=database_path,
                snapshot_path=snapshot_path,
                project_filters=project_filters,
                scrub_preset=scrub_preset,
            )
        except ShareExportError as exc:
            console.print(f"[red]Snapshot preparation failed:[/] {exc}")
            raise typer.Exit(code=1) from exc

        scope = snapshot_ctx.scope
        scrub_summary = snapshot_ctx.scrub_summary
        fts_enabled = snapshot_ctx.fts_enabled
        if not fts_enabled:
            console.print("[yellow]FTS5 not available; viewer will fall back to LIKE search.[/]")
        else:
            console.print("[green]✓ Built FTS5 index for snapshot search.[/]")

        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser()

        export_config = {
            "inline_threshold": inline_threshold,
            "detach_threshold": detach_threshold,
            "chunk_threshold": chunk_threshold,
            "chunk_size": chunk_size,
            "scrub_preset": scrub_preset,
            "projects": project_filters,
        }

        console.print("[cyan]Packaging attachments, viewer assets, and manifest...[/]")
        try:
            bundle_artifacts = build_bundle_assets(
                snapshot_ctx.snapshot_path,
                temp_path,
                storage_root=storage_root,
                inline_threshold=inline_threshold,
                detach_threshold=detach_threshold,
                chunk_threshold=chunk_threshold,
                chunk_size=chunk_size,
                scope=scope,
                project_filters=project_filters,
                scrub_summary=scrub_summary,
                hosting_hints=hosting_hints,
                fts_enabled=fts_enabled,
                export_config=export_config,
            )
        except ShareExportError as exc:
            console.print(f"[red]Failed to build bundle assets:[/] {exc}")
            raise typer.Exit(code=1) from exc

        attachments_manifest = bundle_artifacts.attachments_manifest
        chunk_manifest = bundle_artifacts.chunk_manifest
        if chunk_manifest:
            console.print(
                f"[cyan]Chunked database into {chunk_manifest['chunk_count']} files of ~{chunk_manifest['chunk_size']//1024} KiB.[/]"
            )

        console.print(f"[cyan]Synchronizing updated bundle into:[/] {bundle_path}")
        _copy_bundle_contents(temp_path, bundle_path)

    assert scope is not None and scrub_summary is not None

    if signing_key is not None:
        try:
            public_out_path = _resolve_path(signing_public_out) if signing_public_out else None
            signature_info = sign_manifest(
                bundle_path / "manifest.json",
                signing_key,
                bundle_path,
                public_out=public_out_path,
                overwrite=True,
            )
            console.print(
                f"[green]✓ Signed manifest (Ed25519, public key {signature_info['public_key']})[/]"
            )
        except ShareExportError as exc:
            console.print(f"[red]Manifest signing failed:[/] {exc}")
            raise typer.Exit(code=1) from exc
    elif existing_signature:
        console.print(
            "[yellow]Existing manifest signature may no longer match. Re-run with --signing-key to refresh it.[/]"
        )

    archive_path: Optional[Path] = None
    if zip_bundle:
        archive_path = bundle_path.parent / f"{bundle_path.name}.zip"
        console.print(f"[cyan]Packaging archive:[/] {archive_path}")
        if archive_path.exists():
            console.print(
                f"[red]Archive already exists at {archive_path}. Remove it or specify --no-zip to skip packaging.[/]"
            )
            raise typer.Exit(code=1)
        try:
            package_directory_as_zip(bundle_path, archive_path)
        except ShareExportError as exc:
            console.print(f"[red]Failed to create ZIP archive:[/] {exc}")
            raise typer.Exit(code=1) from exc

    if age_recipient_list:
        if not archive_path:
            console.print("[yellow]Skipped age encryption because --zip was not enabled.[/]")
        else:
            console.print("[cyan]Encrypting archive with age...[/]")
            try:
                encrypted_path = encrypt_bundle(archive_path, age_recipient_list)
                if encrypted_path:
                    console.print(f"[green]✓ Encrypted archive written to {encrypted_path}[/]")
            except ShareExportError as exc:
                console.print(f"[red]age encryption failed:[/] {exc}")
                raise typer.Exit(code=1) from exc

    console.print("[green]✓ Updated SQLite snapshot for sharing.[/]")
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
    if fts_enabled:
        console.print("[green]✓ Built FTS5 index for full-text viewer search.[/]")
    else:
        console.print("[yellow]Search fallback active (FTS5 unavailable in current sqlite build).[/]")
    if chunk_manifest:
        console.print("[green]✓ Chunk manifest refreshed (mailbox.sqlite3.config.json updated).[/]")
        console.print("[dim]Existing chunk files beyond the new chunk count remain on disk; remove them manually if needed.[/]")

    if zip_bundle and archive_path:
        console.print(f"[green]✓ Bundle archive available at {archive_path}[/]")


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

    # Ensure latest viewer assets are present (prefer source tree during dev)
    with suppress(Exception):
        copy_viewer_assets(bundle_path)

    server = _start_preview_server(bundle_path, host, port)
    actual_host, actual_port = server.server_address[:2]
    actual_host = actual_host or host

    console.rule("[bold]Static Bundle Preview[/bold]")
    console.print(f"Serving {bundle_path} at http://{actual_host}:{actual_port}/ (Ctrl+C to stop)")
    console.print("[dim]Commands: press 'r' to force refresh, 'd' to deploy now, 'q' to stop.[/]")

    if open_browser:
        with suppress(Exception):
            webbrowser.open(f"http://{actual_host}:{actual_port}/viewer/")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    running = True
    deployment_requested = False
    try:
        if os.name != "nt" and sys.stdin.isatty():
            import select
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while running and thread.is_alive():
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
                    if rlist:
                        ch = sys.stdin.read(1)
                        if ch in ("\x03", "\x04"):
                            raise KeyboardInterrupt
                        if ch.lower() == "r":
                            token = _bump_preview_force_token()
                            console.print(f"[dim]Reload signal sent (token {token}).[/]")
                        elif ch.lower() == "d":
                            deployment_requested = True
                            running = False
                            break
                        elif ch.lower() == "q":
                            running = False
                            break
                    else:
                        continue
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        else:
            while running and thread.is_alive():
                time.sleep(0.5)
                if os.name == "nt":
                    import msvcrt as _msvcrt

                    while getattr(_msvcrt, "kbhit", lambda: False)():
                        getwch = getattr(_msvcrt, "getwch", None)
                        if getwch is not None:
                            ch = getwch()
                        else:
                            getch = getattr(_msvcrt, "getch", None)
                            if getch is None:
                                break
                            raw = getch()
                            try:
                                ch = raw.decode("utf-8", "ignore")
                            except Exception:
                                ch = str(raw)
                        if ch in ("\x03", "\x1a"):
                            raise KeyboardInterrupt
                        if ch.lower() == "r":
                            token = _bump_preview_force_token()
                            console.print(f"[dim]Reload signal sent (token {token}).[/]")
                        elif ch.lower() == "d":
                            deployment_requested = True
                            running = False
                            break
                        elif ch.lower() == "q":
                            running = False
                            break
                else:
                    continue
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down preview server...[/]")
    finally:
        if not running:
            console.print("\n[dim]Stopping preview server (requested).[/]")
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        console.print("[green]Preview server stopped.[/]")
        if deployment_requested:
            # Special exit code indicates deployment requested by user from preview
            raise typer.Exit(code=42)


@share_app.command("verify")
def share_verify(
    bundle: Annotated[str, typer.Argument(help="Path to the exported bundle directory.")],
    public_key: Annotated[
        Optional[str],
        typer.Option(
            "--public-key",
            help="Ed25519 public key (base64) to verify signature. If omitted, uses key from manifest.sig.json.",
        ),
    ] = None,
) -> None:
    """Verify bundle integrity (SRI hashes) and optional Ed25519 signature."""
    from .share import verify_bundle

    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists():
        console.print(f"[red]Bundle directory not found:[/] {bundle_path}")
        raise typer.Exit(code=1)
    if not bundle_path.is_dir():
        console.print(f"[red]Bundle path must be a directory:[/] {bundle_path}")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Verifying bundle:[/] {bundle_path}")

    try:
        result = verify_bundle(bundle_path, public_key=public_key)
        console.print()
        console.print("[green]✓ Bundle verification passed[/]")
        console.print(f"  Bundle: {result['bundle']}")
        console.print(f"  SRI checked: {result['sri_checked']}")
        console.print(f"  Signature checked: {result['signature_checked']}")
        console.print(f"  Signature verified: {result['signature_verified']}")

        if not result["sri_checked"]:
            console.print("[yellow]  Warning: No SRI hashes found in manifest[/]")
        if not result["signature_checked"]:
            console.print("[yellow]  Warning: No signature found (manifest.sig.json)[/]")

    except ShareExportError as exc:
        console.print(f"[red]Verification failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


@share_app.command("decrypt")
def share_decrypt(
    encrypted_path: Annotated[str, typer.Argument(help="Path to the age-encrypted file (e.g., bundle.zip.age).")],
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Path where decrypted file should be written. Defaults to encrypted filename with .age removed.",
        ),
    ] = None,
    identity: Annotated[
        Optional[Path],
        typer.Option(
            "--identity",
            "-i",
            help="Path to age identity file (private key). Mutually exclusive with --passphrase.",
        ),
    ] = None,
    passphrase: Annotated[
        bool,
        typer.Option(
            "--passphrase",
            "-p",
            help="Prompt for passphrase interactively. Mutually exclusive with --identity.",
        ),
    ] = False,
) -> None:
    """Decrypt an age-encrypted bundle using identity file or passphrase."""
    from .share import decrypt_with_age

    enc_path = _resolve_path(encrypted_path)
    if not enc_path.exists():
        console.print(f"[red]Encrypted file not found:[/] {enc_path}")
        raise typer.Exit(code=1)
    if not enc_path.is_file():
        console.print(f"[red]Encrypted path must be a file, not a directory:[/] {enc_path}")
        raise typer.Exit(code=1)

    # Auto-determine output path if not provided
    if output is None:
        if enc_path.suffix == ".age":
            out_path = enc_path.with_suffix("")
        else:
            out_path = enc_path.parent / f"{enc_path.stem}_decrypted{enc_path.suffix}"
    else:
        out_path = _resolve_path(output)

    passphrase_text: Optional[str] = None
    if passphrase:
        import getpass

        passphrase_text = getpass.getpass("Enter passphrase: ")
        if not passphrase_text:
            console.print("[red]Passphrase cannot be empty[/]")
            raise typer.Exit(code=1)

    console.print(f"[cyan]Decrypting:[/] {enc_path} → {out_path}")

    try:
        decrypt_with_age(
            enc_path,
            out_path,
            identity=identity,
            passphrase=passphrase_text,
        )
        console.print(f"[green]✓ Decrypted successfully to {out_path}[/]")
    except ShareExportError as exc:
        console.print(f"[red]Decryption failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


@share_app.command("wizard")
def share_wizard() -> None:
    """Launch interactive deployment wizard for GitHub Pages or Cloudflare Pages."""
    console.print("[cyan]Launching deployment wizard...[/]")

    # Import and run the wizard script
    import subprocess
    import sys

    # Try to find wizard script - first check if running from source
    wizard_script = Path(__file__).parent.parent.parent / "scripts" / "share_to_github_pages.py"

    if not wizard_script.exists():
        # If not in source tree, check if it's in the same directory as this module (for editable installs)
        alt_path = Path(__file__).parent / "scripts" / "share_to_github_pages.py"
        if alt_path.exists():
            wizard_script = alt_path
        else:
            console.print("[red]Wizard script not found.[/]")
            console.print("[yellow]Expected locations:[/]")
            console.print(f"  • {wizard_script}")
            console.print(f"  • {alt_path}")
            console.print("\n[yellow]This command only works when running from source.[/]")
            console.print("[cyan]Run the wizard directly:[/] python scripts/share_to_github_pages.py")
            raise typer.Exit(code=1)

    try:
        # Run the wizard script as a subprocess so it can handle its own console interactions
        result = subprocess.run([sys.executable, str(wizard_script)], check=False)
        raise typer.Exit(code=result.returncode)
    except KeyboardInterrupt:
        console.print("\n[yellow]Wizard cancelled by user[/]")
        raise typer.Exit(code=0) from None


def _resolve_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    return path


@dataclass(slots=True)
class StoredExportConfig:
    projects: list[str]
    inline_threshold: int
    detach_threshold: int
    chunk_threshold: int
    chunk_size: int
    scrub_preset: str


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_bundle_export_config(bundle_dir: Path) -> StoredExportConfig:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise ShareExportError(f"manifest.json not found in {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShareExportError(f"Failed to parse manifest.json: {exc}") from exc

    export_config = manifest.get("export_config", {}) or {}
    attachments_section = manifest.get("attachments", {}) or {}
    attachments_config = attachments_section.get("config", {}) or {}
    project_scope = manifest.get("project_scope", {}) or {}
    scrub_section = manifest.get("scrub", {}) or {}
    database_section = manifest.get("database", {}) or {}

    raw_projects = export_config.get("projects", project_scope.get("requested", []))
    projects = [str(p) for p in raw_projects if isinstance(p, str)]

    scrub_preset = str(export_config.get("scrub_preset") or scrub_section.get("preset") or "standard")

    inline_threshold = _coerce_int(
        export_config.get("inline_threshold") or attachments_config.get("inline_threshold"),
        INLINE_ATTACHMENT_THRESHOLD,
    )
    detach_threshold = _coerce_int(
        export_config.get("detach_threshold") or attachments_config.get("detach_threshold"),
        DETACH_ATTACHMENT_THRESHOLD,
    )
    chunk_threshold = _coerce_int(export_config.get("chunk_threshold"), DEFAULT_CHUNK_THRESHOLD)

    chunk_manifest = database_section.get("chunk_manifest") or {}
    chunk_size = _coerce_int(
        export_config.get("chunk_size") or chunk_manifest.get("chunk_size"),
        DEFAULT_CHUNK_SIZE,
    )

    chunk_config: dict[str, Any] = {}
    chunk_config_path = bundle_dir / "mailbox.sqlite3.config.json"
    if chunk_config_path.exists():
        try:
            chunk_config = json.loads(chunk_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            chunk_config = {}
    if chunk_config:
        chunk_size = _coerce_int(chunk_config.get("chunk_size"), chunk_size)
        chunk_threshold = _coerce_int(chunk_config.get("threshold_bytes"), chunk_threshold)

    return StoredExportConfig(
        projects=projects,
        inline_threshold=inline_threshold,
        detach_threshold=detach_threshold,
        chunk_threshold=chunk_threshold,
        chunk_size=chunk_size,
        scrub_preset=scrub_preset,
    )


def _copy_bundle_contents(source: Path, destination: Path) -> None:
    """Synchronise *destination* with *source* by mirroring files and pruning stale artefacts."""

    source = source.resolve()
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    desired_files: set[Path] = set()
    desired_dirs: set[Path] = {destination}

    for root, _, files in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        dest_root = destination / relative_root
        desired_dirs.add(dest_root)
        for filename in files:
            rel_file = relative_root / filename
            desired_files.add(destination / rel_file)
            parent = (destination / rel_file).parent
            while parent != destination:
                desired_dirs.add(parent)
                parent = parent.parent

    # Remove files that are no longer present in the source bundle.
    existing_files = {path for path in destination.rglob("*") if path.is_file()}
    for stale_file in existing_files - desired_files:
        # Unlink without following symlinks (we never export symlinks, but be defensive).
        stale_file.unlink(missing_ok=True)

    # Remove directories that are no longer needed (deepest first).
    existing_dirs = {path for path in destination.rglob("*") if path.is_dir()}
    for stale_dir in sorted(existing_dirs - desired_dirs, key=lambda p: len(p.parts), reverse=True):
        with suppress(OSError):
            stale_dir.rmdir()

    # Copy fresh files from source (overwrite in place to handle updated content).
    for root, _, files in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        dest_root = destination / relative_root
        dest_root.mkdir(parents=True, exist_ok=True)
        for filename in files:
            src_file = root_path / filename
            dest_file = dest_root / filename
            shutil.copy2(src_file, dest_file)


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
def list_projects(
    include_agents: bool = typer.Option(False, help="Include agent counts."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON for machine parsing."),
) -> None:
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

    if not json_output:
        with console.status("Collecting project data..."):
            rows = asyncio.run(_collect())
    else:
        rows = asyncio.run(_collect())

    if json_output:
        # Machine-readable JSON output
        projects_json = []
        for project, agent_count in rows:
            entry = {
                "id": project.id,
                "slug": project.slug,
                "human_key": project.human_key,
                "created_at": project.created_at.isoformat(),
            }
            if include_agents:
                entry["agent_count"] = agent_count
            projects_json.append(entry)
        import sys
        json.dump(projects_json, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        # Human-readable Rich table output
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
    prepush: Annotated[bool, typer.Option("--prepush/--no-prepush", help="Also install a pre-push guard.",)] = False,
) -> None:
    """Install the advisory pre-commit guard into the given repository."""

    settings = get_settings()
    if not settings.worktrees_enabled:
        console.print("[yellow]Worktree-friendly features are disabled (WORKTREES_ENABLED=0). Skipping guard install.[/]")
        return
    repo_path = repo.expanduser().resolve()

    async def _run() -> tuple[Project, Path]:
        project_record = await _get_project_record(project)
        hook_path = await install_guard_script(settings, project_record.slug, repo_path)
        if prepush:
            try:
                from .guard import install_prepush_guard as _install_prepush
                await _install_prepush(settings, project_record.slug, repo_path)
            except Exception as exc:
                console.print(f"[yellow]Warning: failed to install pre-push guard: {exc}[/]")
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
    # Resolve hooks directory for accurate messaging
    def _git(cwd: Path, *args: str) -> str | None:
        try:
            cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
            return cp.stdout.strip()
        except Exception:
            return None
    hooks_path = _git(repo_path, "config", "--get", "core.hooksPath")
    if hooks_path:
        if hooks_path.startswith("/") or (((((len(hooks_path) > 1) and (hooks_path[1:3] == ":\\")) or (hooks_path[1:3] == ":/")))):
            hooks_dir = Path(hooks_path)
        else:
            root = _git(repo_path, "rev-parse", "--show-toplevel") or str(repo_path)
            hooks_dir = Path(root) / hooks_path
    else:
        git_dir = _git(repo_path, "rev-parse", "--git-dir") or ".git"
        g = Path(git_dir)
        if not g.is_absolute():
            g = repo_path / g
        hooks_dir = g / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"
    if removed:
        console.print(f"[green]Removed guard scripts at {pre_commit} and (if present) {pre_push}.")
    else:
        console.print(f"[yellow]No guard scripts found at {hooks_dir}.")


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

@amctl_app.command("env")
def amctl_env(
    project_path: Annotated[Path, typer.Option("--path", "-p", help="Path to repo/worktree",)] = Path(),
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent name (defaults to $AGENT_NAME)")] = None,
) -> None:
    """
    Print environment variables useful for build wrappers (slots, caches, artifacts).
    """
    p = project_path.expanduser().resolve()
    agent_name = agent or os.environ.get("AGENT_NAME") or "Unknown"
    # Reuse server helper for identity
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident  # type: ignore
    ident = _resolve_ident(str(p))
    slug = ident["slug"]
    project_uid = ident["project_uid"]
    # Determine branch
    branch = ident.get("branch") or ""
    if not branch:
        try:
            from git import Repo as _Repo
            repo = _Repo(str(p), search_parent_directories=True)
            try:
                branch = repo.active_branch.name
            except Exception:
                branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
        except Exception:
            branch = "unknown"
    # Compute cache key and artifact dir
    settings = get_settings()
    cache_key = f"am-cache-{project_uid}-{agent_name}-{branch}"
    artifact_dir = Path(settings.storage.root).expanduser().resolve() / "projects" / slug / "artifacts" / agent_name / branch
    # Print as KEY=VALUE lines
    console.print(f"SLUG={slug}")
    console.print(f"PROJECT_UID={project_uid}")
    console.print(f"BRANCH={branch}")
    console.print(f"AGENT={agent_name}")
    console.print(f"CACHE_KEY={cache_key}")
    console.print(f"ARTIFACT_DIR={artifact_dir}")


@app.command(name="am-run")
def am_run(
    slot: Annotated[str, typer.Argument(help="Build slot name (e.g., frontend-build)")],
    cmd: Annotated[list[str], typer.Argument(help="Command to run")],
    project_path: Annotated[Path, typer.Option("--path", "-p", help="Path to repo/worktree",)] = Path(),
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent name (defaults to $AGENT_NAME)")] = None,
    ttl_seconds: Annotated[int, typer.Option("--ttl-seconds", help="Lease TTL seconds (default 3600)")] = 3600,
    shared: Annotated[bool, typer.Option("--shared/--exclusive", help="Shared (non-exclusive) lease",)] = False,
    block_on_conflicts: Annotated[bool, typer.Option("--block-on-conflicts/--no-block-on-conflicts", help="Exit 1 if exclusive conflicts are present")] = False,
) -> None:
    """
    Build wrapper that prepares environment variables and manages a build slot:
    - Acquires the slot (advisory), prints conflicts in warn mode.
    - Renews lease in the background while the child runs.
    - Releases the slot on exit.
    """
    p = project_path.expanduser().resolve()
    agent_name = agent or os.environ.get("AGENT_NAME") or "Unknown"
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident  # type: ignore
    ident = _resolve_ident(str(p))
    slug = ident["slug"]
    project_uid = ident["project_uid"]
    branch = ident.get("branch") or ""
    # Always ensure archive structure exists (for tests and local runs)
    asyncio.run(ensure_archive(get_settings(), slug))
    if not branch:
        try:
            from git import Repo as _Repo
            repo = _Repo(str(p), search_parent_directories=True)
            try:
                branch = repo.active_branch.name
            except Exception:
                branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
        except Exception:
            branch = "unknown"
    settings = get_settings()
    guard_mode = (os.environ.get("AGENT_MAIL_GUARD_MODE", "block") or "block").strip().lower()
    worktrees_enabled = bool(settings.worktrees_enabled)
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    bearer = settings.http.bearer_token or os.environ.get("HTTP_BEARER_TOKEN", "")

    def _safe_component(value: str) -> str:
        s = value.strip()
        for ch in ("/", "\\\\", ":", "*", "?", "\"", "<", ">", "|", " "):
            s = s.replace(ch, "_")
        return s or "unknown"

    async def _ensure_slot_paths() -> Path:
        archive = await ensure_archive(settings, slug)
        slot_dir = archive.root / "build_slots" / _safe_component(slot)
        slot_dir.mkdir(parents=True, exist_ok=True)
        return slot_dir

    def _read_active(slot_dir: Path) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        for f in slot_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                exp = data.get("expires_ts")
                if exp:
                    try:
                        if datetime.fromisoformat(exp) <= now:
                            continue
                    except Exception:
                        pass
                results.append(data)
            except Exception:
                continue
        return results

    def _lease_path(slot_dir: Path) -> Path:
        holder = _safe_component(f"{agent_name}__{branch or 'unknown'}")
        return slot_dir / f"{holder}.json"
    # Ensure local lease path exists upfront so tests can observe it even if server path is used
    lease_path: Optional[Path] = None
    try:
        slot_dir_eager = asyncio.run(_ensure_slot_paths())
        lease_path = _lease_path(slot_dir_eager)
        payload = {
            "slot": slot,
            "agent": agent_name,
            "branch": branch,
            "exclusive": (not shared),
            "acquired_ts": datetime.now(timezone.utc).isoformat(),
            "expires_ts": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
        }
        with suppress(Exception):
            lease_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass
    env = os.environ.copy()
    env.update({
        "AM_SLOT": slot,
        "SLUG": slug,
        "PROJECT_UID": project_uid or "",
        "BRANCH": branch,
        "AGENT": agent_name,
        "CACHE_KEY": f"am-cache-{project_uid}-{agent_name}-{branch}",
    })
    # lease_path may already be set by eager creation above
    renew_stop = threading.Event()
    renew_thread: Optional[threading.Thread] = None
    try:
        if worktrees_enabled:
            # Prefer server tools (authority); fallback to local FS leases
            use_server = True
            try:
                with httpx.Client(timeout=5.0) as client:
                    headers = {}
                    if bearer:
                        headers["Authorization"] = f"Bearer {bearer}"
                    req = {
                        "jsonrpc": "2.0",
                        "id": "am-run-ensure",
                        "method": "tools/call",
                        "params": {"name": "ensure_project", "arguments": {"human_key": str(p)}},
                    }
                    client.post(server_url, json=req, headers=headers)
            except Exception:
                use_server = False

            if use_server:
                conflicts: list[dict[str, Any]] = []
                try:
                    with httpx.Client(timeout=5.0) as client:
                        headers = {}
                        if bearer:
                            headers["Authorization"] = f"Bearer {bearer}"
                        req = {
                            "jsonrpc": "2.0",
                            "id": "am-run-acquire",
                            "method": "tools/call",
                            "params": {
                                "name": "acquire_build_slot",
                                "arguments": {
                                    "project_key": str(p),
                                    "agent_name": agent_name,
                                    "slot": slot,
                                    "ttl_seconds": int(ttl_seconds),
                                    "exclusive": (not shared),
                                },
                            },
                        }
                        resp = client.post(server_url, json=req, headers=headers)
                        data = resp.json()
                        result = (data or {}).get("result") or {}
                        conflicts = list(result.get("conflicts") or [])
                except Exception:
                    use_server = False

                if conflicts and guard_mode == "warn":
                    console.print("[yellow]Build slot conflicts (server advisory, proceeding):[/]")
                    for c in conflicts:
                        console.print(
                            f"  - slot={c.get('slot','')} agent={c.get('agent','')} "
                            f"branch={c.get('branch','')} expires={c.get('expires_ts','')}"
                        )
                if conflicts and (not shared) and block_on_conflicts:
                    console.print("[red]Build slot conflicts detected and --block-on-conflicts set; aborting.[/]")
                    raise typer.Exit(code=1)

                def _renewer_srv() -> None:
                    interval = max(60, ttl_seconds // 2)
                    while not renew_stop.wait(interval):
                        try:
                            with httpx.Client(timeout=5.0) as client:
                                headers = {}
                                if bearer:
                                    headers["Authorization"] = f"Bearer {bearer}"
                                req = {
                                    "jsonrpc": "2.0",
                                    "id": "am-run-renew",
                                    "method": "tools/call",
                                    "params": {
                                        "name": "renew_build_slot",
                                        "arguments": {
                                            "project_key": str(p),
                                            "agent_name": agent_name,
                                            "slot": slot,
                                            "extend_seconds": max(60, ttl_seconds // 2),
                                        },
                                    },
                                }
                                client.post(server_url, json=req, headers=headers)
                        except Exception:
                            continue

                renew_thread = threading.Thread(target=_renewer_srv, name="am-run-renew", daemon=True)
                renew_thread.start()

            if not use_server:
                slot_dir = asyncio.run(_ensure_slot_paths())
                active = _read_active(slot_dir)
                conflicts = [
                    e for e in active
                    if e.get("exclusive", True) and not shared and not (e.get("agent") == agent_name and e.get("branch") == branch)
                ]
                if conflicts and guard_mode == "warn":
                    console.print("[yellow]Build slot conflicts (advisory, proceeding):[/]")
                    for c in conflicts:
                        console.print(
                            f"  - slot={c.get('slot','')} agent={c.get('agent','')} "
                            f"branch={c.get('branch','')} expires={c.get('expires_ts','')}"
                        )
                if conflicts and (not shared) and block_on_conflicts:
                    console.print("[red]Build slot conflicts detected and --block-on-conflicts set; aborting.[/]")
                    raise typer.Exit(code=1)
                lease_path = _lease_path(slot_dir)
                payload = {
                    "slot": slot,
                    "agent": agent_name,
                    "branch": branch,
                    "exclusive": (not shared),
                    "acquired_ts": datetime.now(timezone.utc).isoformat(),
                    "expires_ts": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
                }
                with suppress(Exception):
                    lease_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

                def _renewer() -> None:
                    interval = max(60, ttl_seconds // 2)
                    while not renew_stop.wait(interval):
                        try:
                            now = datetime.now(timezone.utc)
                            new_exp = now + timedelta(seconds=max(60, ttl_seconds // 2))
                            try:
                                current = json.loads(lease_path.read_text(encoding="utf-8")) if lease_path else {}
                            except Exception:
                                current = {}
                            current.update({"expires_ts": new_exp.isoformat()})
                            if lease_path:
                                lease_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
                        except Exception:
                            continue
                renew_thread = threading.Thread(target=_renewer, name="am-run-renew", daemon=True)
                renew_thread.start()
        console.print(f"[cyan]$ {' '.join(cmd)}[/]  [dim](slot={slot})[/]")
        rc = subprocess.run(list(cmd), env=env, check=False).returncode
    except FileNotFoundError:
        rc = 127
    finally:
        if worktrees_enabled:
            # Attempt server release; fallback to local lease release
            try:
                with httpx.Client(timeout=5.0) as client:
                    headers = {}
                    if bearer:
                        headers["Authorization"] = f"Bearer {bearer}"
                    req = {
                        "jsonrpc": "2.0",
                        "id": "am-run-release",
                        "method": "tools/call",
                        "params": {
                            "name": "release_build_slot",
                            "arguments": {"project_key": str(p), "agent_name": agent_name, "slot": slot},
                        },
                    }
                    client.post(server_url, json=req, headers=headers)
            except Exception:
                if lease_path:
                    try:
                        now = datetime.now(timezone.utc)
                        try:
                            data = json.loads(lease_path.read_text(encoding="utf-8"))
                        except Exception:
                            data = {}
                        data.update({"released_ts": now.isoformat(), "expires_ts": now.isoformat()})
                        with suppress(Exception):
                            lease_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            finally:
                renew_stop.set()
                if renew_thread and renew_thread.is_alive():
                    renew_thread.join(timeout=1.0)
    if rc != 0:
        raise typer.Exit(code=rc)

@projects_app.command("mark-identity")
def projects_mark_identity(
    project_path: Annotated[Path, typer.Argument(..., help="Path to repo/worktree ('.' for current)")],
    commit: Annotated[bool, typer.Option("--commit/--no-commit", help="Write committed marker .agent-mail-project-id")] = True,
) -> None:
    """
    Write the current project_uid into a marker file (.agent-mail-project-id).
    """
    p = project_path.expanduser().resolve()
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident  # type: ignore
    ident = _resolve_ident(str(p))
    uid = ident.get("project_uid") or ""
    if not uid:
        raise typer.BadParameter("Unable to resolve project_uid for this path.")
    # Determine repo root
    try:
        from git import Repo as _Repo
        repo = _Repo(str(p), search_parent_directories=True)
        root = Path(repo.working_tree_dir or str(p))
    except Exception:
        root = p
    marker_path = root / ".agent-mail-project-id"
    marker_path.write_text(uid + "\n", encoding="utf-8")
    console.print(f"[green]Wrote[/] {marker_path} with project_uid={uid}")
    if commit:
        try:
            subprocess.run(["git", "-C", str(root), "add", str(marker_path)], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "chore: add .agent-mail-project-id"], check=True)
            console.print("[green]Committed marker file.[/]")
        except Exception:
            console.print("[yellow]Unable to commit marker automatically. Please commit manually.[/]")


@projects_app.command("discovery-init")
def projects_discovery_init(
    project_path: Annotated[Path, typer.Argument(..., help="Path to repo/worktree ('.' for current)")],
    product: Annotated[Optional[str], typer.Option("--product", "-P", help="Optional product_uid")] = None,
) -> None:
    """
    Scaffold a discovery YAML file (.agent-mail.yaml) with project_uid (and optional product_uid).
    """
    p = project_path.expanduser().resolve()
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident  # type: ignore
    ident = _resolve_ident(str(p))
    uid = ident.get("project_uid") or ""
    if not uid:
        raise typer.BadParameter("Unable to resolve project_uid for this path.")
    ypath = p / ".agent-mail.yaml"
    lines = ["# Agent Mail discovery file", f"project_uid: {uid}"]
    if product:
        lines.append(f"product_uid: {product}")
    ypath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Wrote[/] {ypath}")
@mail_app.command("status")
def mail_status(
    project_path: Annotated[
        Path,
        typer.Argument(..., help="Absolute path to a repo/worktree directory (use '.' for current)."),
    ],
) -> None:
    """
    Print routing diagnostics: gate state, configured identity mode, normalized remote (if any),
    and the slug that would be used for this path.
    """
    settings = get_settings()
    p = project_path.expanduser().resolve()
    gate = settings.worktrees_enabled
    mode = (settings.project_identity_mode or "dir").strip().lower()
    remote_name = (settings.project_identity_remote or "origin").strip()

    def _norm_remote(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        u = url.strip()
        try:
            if u.startswith("git@"):
                host = u.split("@", 1)[1].split(":", 1)[0]
                path = u.split(":", 1)[1]
            else:
                from urllib.parse import urlparse as _urlparse
                pr = _urlparse(u)
                host = pr.hostname or ""
                path = (pr.path or "")
        except Exception:
            return None
        if not host:
            return None
        path = path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = [seg for seg in path.split("/") if seg]
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        return f"{host}/{owner}/{repo}"

    normalized_remote: Optional[str] = None
    try:
        from git import Repo as _Repo  # local import to avoid CLI startup cost
        repo = _Repo(str(p), search_parent_directories=True)
        try:
            url = repo.git.remote("get-url", remote_name).strip() or None
        except Exception:
            try:
                r = next((r for r in repo.remotes if r.name == remote_name), None)
                url = next(iter(r.urls), None) if r and r.urls else None
            except Exception:
                url = None
        normalized_remote = _norm_remote(url)
    except Exception:
        normalized_remote = None

    # Compute a candidate slug using the same logic as the server helper (summarized)
    from mcp_agent_mail.app import _compute_project_slug as _compute_slug  # type: ignore
    slug_value = _compute_slug(str(p))

    table = Table(title="Mail routing status", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("WORKTREES_ENABLED", "true" if gate else "false")
    table.add_row("PROJECT_IDENTITY_MODE", mode or "dir")
    table.add_row("PROJECT_IDENTITY_REMOTE", remote_name)
    table.add_row("normalized_remote", normalized_remote or "")
    table.add_row("slug", slug_value)
    table.add_row("path", str(p))
    console.print(table)


@guard_app.command("status")
def guard_status(
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
) -> None:
    """
    Print guard status: gate/mode, resolved hooks directory, and presence of hooks.
    """
    settings = get_settings()
    p = repo.expanduser().resolve()
    gate = settings.worktrees_enabled
    mode = (settings.project_identity_mode or "dir").strip().lower()
    guard_mode = (os.environ.get("AGENT_MAIL_GUARD_MODE", "block") or "block").strip().lower()

    def _git(cwd: Path, *args: str) -> str | None:
        try:
            cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
            return cp.stdout.strip()
        except Exception:
            return None

    hooks_path = _git(p, "config", "--get", "core.hooksPath")
    if hooks_path:
        if hooks_path.startswith("/") or ((((len(hooks_path) > 1) and (hooks_path[1:3] == ":\\")) or (hooks_path[1:3] == ":/"))):
            hooks_dir = Path(hooks_path)
        else:
            root = _git(p, "rev-parse", "--show-toplevel") or str(p)
            hooks_dir = Path(root) / hooks_path
    else:
        git_dir = _git(p, "rev-parse", "--git-dir") or ".git"
        g = Path(git_dir)
        if not g.is_absolute():
            g = p / g
        hooks_dir = g / "hooks"

    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"

    table = Table(title="Guard status", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("WORKTREES_ENABLED", "true" if gate else "false")
    table.add_row("AGENT_MAIL_GUARD_MODE", guard_mode)
    table.add_row("PROJECT_IDENTITY_MODE", mode or "dir")
    table.add_row("hooks_dir", str(hooks_dir))
    table.add_row("pre-commit", "present" if pre_commit.exists() else "missing")
    table.add_row("pre-push", "present" if pre_push.exists() else "missing")
    console.print(table)

@guard_app.command("check")
def guard_check(
    stdin_nul: bool = typer.Option(False, "--stdin-nul", help="Read NUL-delimited paths from STDIN"),
    advisory: bool = typer.Option(False, "--advisory", help="Advisory mode: print conflicts but exit 0"),
    repo: Annotated[Optional[Path], typer.Option("--repo", help="Path to git repo (defaults to detected root)")] = None,
) -> None:
    """
    Check paths (from STDIN when --stdin-nul) against active exclusive file_reservations.

    Unifies guard semantics across hooks and CLI:
    - Normalizes paths to repo-root relative, honoring core.ignorecase
    - Uses Git wildmatch semantics via pathspec when available, with fnmatch fallback
    - Prints conflicts and returns non-zero unless --advisory is set
    """
    settings = get_settings()
    agent_name = os.environ.get("AGENT_NAME")
    if not agent_name:
        console.print("[red]AGENT_NAME environment variable is required.[/]")
        raise typer.Exit(code=1)

    def _git(cwd: Path, *args: str) -> str | None:
        try:
            cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
            return cp.stdout.strip()
        except Exception:
            return None

    if repo is not None:
        repo_root = repo.expanduser().resolve()
    else:
        guess = _git(Path.cwd(), "rev-parse", "--show-toplevel")
        repo_root = Path(guess).expanduser().resolve() if guess else Path.cwd().expanduser().resolve()

    # Map repo path to project archive
    try:
        from mcp_agent_mail.app import _compute_project_slug as _compute_slug  # type: ignore
    except Exception:
        console.print("[red]Internal error: cannot import slug helper.[/]")
        raise typer.Exit(code=1) from None
    slug_value = _compute_slug(str(repo_root))
    archive = asyncio.run(ensure_archive(settings, slug_value))

    # Read NUL-delimited paths from STDIN
    paths: list[str] = []
    if stdin_nul:
        data = sys.stdin.buffer.read()
        if data:
            items = [p for p in data.decode("utf-8", "ignore").split("\x00") if p]
            # De-duplicate while preserving order
            seen = set()
            for p in items:
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
    if not paths:
        raise typer.Exit(code=0)

    # Matching semantics
    ignorecase = False
    ic = _git(repo_root, "config", "--get", "core.ignorecase")
    if ic and ic.strip().lower() == "true":
        ignorecase = True
    try:
        from pathspec import PathSpec as _PS  # type: ignore
    except Exception:
        _PS = None  # type: ignore
    import fnmatch as _fn

    def _normalize(p: str) -> str:
        s = p.replace("\\", "/").lstrip("/")
        return s.lower() if ignorecase else s

    def _compile(pattern: str):
        patt = pattern.lower() if ignorecase else pattern
        if _PS is not None:
            try:
                return _PS.from_lines("gitwildmatch", [patt])
            except Exception:
                return None
        return None

    def _match(spec, a: str, b: str) -> bool:
        aa = _normalize(a)
        bb = _normalize(b)
        if spec is not None:
            try:
                return bool(spec.match_file(aa))
            except Exception:
                pass
        return _fn.fnmatchcase(aa, bb) or _fn.fnmatchcase(bb, aa) or (aa == bb)

    fr_dir = archive.root / "file_reservations"
    if not fr_dir.exists():
        raise typer.Exit(code=0)
    now = datetime.now(timezone.utc)
    conflicts: list[tuple[str, str, str]] = []

    for candidate in sorted(fr_dir.glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("agent") == agent_name:
            continue
        if not data.get("exclusive", True):
            continue
        expires = data.get("expires_ts")
        if expires:
            try:
                if datetime.fromisoformat(expires) < now:
                    continue
            except Exception:
                pass
        pattern = (data.get("path_pattern") or "").strip()
        if not pattern:
            continue
        spec = _compile(pattern)
        for path_value in paths:
            if _match(spec, path_value, pattern):
                conflicts.append((path_value, data.get("agent", ""), pattern))

    if conflicts:
        console.print("[red]Exclusive file_reservation conflicts detected:[/]")
        for path_value, agent, pattern in conflicts:
            console.print(f"  - {path_value} matches file_reservation '{pattern}' held by [bold]{agent}[/]")
        if advisory:
            console.print("[yellow]Advisory mode: not blocking (set AGENT_MAIL_GUARD_MODE=block to enforce).[/]")
            raise typer.Exit(code=0)
        else:
            console.print("[yellow]Resolve conflicts or release file_reservations before proceeding.[/]")
            console.print("[dim]Hints: set AGENT_MAIL_GUARD_MODE=warn for advisory, or AGENT_MAIL_BYPASS=1 to bypass in emergencies.[/]")
            raise typer.Exit(code=1)
    raise typer.Exit(code=0)

@projects_app.command("adopt")
def projects_adopt(
    source: Annotated[str, typer.Argument(..., help="Old project slug or human key")],
    target: Annotated[str, typer.Argument(..., help="New project slug or project_uid (future)")],
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply", help="Show plan without applying changes.")] = True,
) -> None:
    """
    Plan and optionally apply consolidation of legacy per-worktree projects into a canonical project.
    """
    async def _load(slug_or_key: str) -> Project:
        return await _get_project_record(slug_or_key)

    try:
        async def _both() -> tuple[Project, Project]:
            return await asyncio.gather(_load(source), _load(target))  # type: ignore[return-value]
        src, dst = asyncio.run(_both())
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc

    if src.id == dst.id:
        console.print("[yellow]Source and target refer to the same project; nothing to do.[/]")
        return

    plan: list[str] = []
    plan.append(f"Source: id={src.id} slug={src.slug} key={src.human_key}")
    plan.append(f"Target: id={dst.id} slug={dst.slug} key={dst.human_key}")

    # Heuristic: same repo if git-common-dir hashes match
    def _git(path: Path, *args: str) -> str | None:
        try:
            cp = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
            return cp.stdout.strip()
        except Exception:
            return None

    src_gdir = _git(Path(src.human_key), "rev-parse", "--git-common-dir")
    dst_gdir = _git(Path(dst.human_key), "rev-parse", "--git-common-dir")
    same_repo = bool(src_gdir and dst_gdir and Path(src_gdir).resolve() == Path(dst_gdir).resolve())
    plan.append(f"Same repo (git-common-dir): {'yes' if same_repo else 'no'}")

    if not same_repo:
        console.print("[red]Refusing to adopt: projects do not appear to belong to the same repository.[/]")
        return

    # Describe filesystem moves (archive layout)
    settings = get_settings()
    from .storage import ensure_archive as _ensure_archive
    src_archive = asyncio.run(_ensure_archive(settings, src.slug))
    dst_archive = asyncio.run(_ensure_archive(settings, dst.slug))
    plan.append(f"Move Git artifacts: {src_archive.root} -> {dst_archive.root}")
    plan.append("Re-key DB rows: source project_id -> target project_id (messages, agents, file_reservations, etc.)")
    plan.append("Write aliases.json under target 'projects/<slug>/' with former_slugs")

    console.print("[bold]Projects adopt plan (dry-run)[/bold]")
    for line in plan:
        console.print(f"- {line}")

    if dry_run:
        return
    # Apply phase
    async def _apply() -> None:
        if src.id is None or dst.id is None:
            raise typer.BadParameter("Projects must be persisted (id not null).")
        # Detect agent name conflicts
        await ensure_schema()
        async with get_session() as session:
            src_agents = [row[0] for row in (await session.execute(select(Agent.name).where(Agent.project_id == src.id))).all()]
            dst_agents = [row[0] for row in (await session.execute(select(Agent.name).where(Agent.project_id == dst.id))).all()]
            dup = sorted(set(src_agents).intersection(set(dst_agents)))
            if dup:
                raise typer.BadParameter(f"Agent name conflicts in target project: {', '.join(dup)}")
        # Move Git artifacts
        settings = get_settings()
        # local import to minimize top-level churn and keep ordering stable
        from .storage import AsyncFileLock as _AsyncFileLock, ensure_archive as _ensure_archive  # type: ignore
        src_archive = asyncio.run(_ensure_archive(settings, src.slug))
        dst_archive = asyncio.run(_ensure_archive(settings, dst.slug))
        moved_relpaths: list[str] = []
        for path in sorted(src_archive.root.rglob("*"), key=str):
            if not path.is_file():
                continue
            if path.name.endswith(".lock") or path.name.endswith(".lock.owner.json"):
                continue
            rel_from_root = path.relative_to(src_archive.root)
            dest_path = dst_archive.root / rel_from_root
            await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)
            if dest_path.exists():
                continue
            await asyncio.to_thread(path.replace, dest_path)
            moved_relpaths.append(dest_path.relative_to(dst_archive.repo_root).as_posix())
        from .storage import _commit as _archive_commit  # type: ignore
        async with _AsyncFileLock(dst_archive.lock_path):
            await _archive_commit(dst_archive.repo, settings, f"adopt: move {src.slug} into {dst.slug}", moved_relpaths)
        # Re-key database rows (agents, messages, file_reservations)
        async with get_session() as session:
            from sqlalchemy import update as _update  # local import to avoid top-of-file churn
            await session.execute(_update(Agent).where(Agent.project_id == src.id).values(project_id=dst.id))
            await session.execute(_update(Message).where(Message.project_id == src.id).values(project_id=dst.id))
            await session.execute(_update(FileReservation).where(FileReservation.project_id == src.id).values(project_id=dst.id))
            await session.commit()
        # Write aliases.json under target
        aliases_path = dst_archive.root / "aliases.json"
        try:
            existing = {}
            if aliases_path.exists():
                existing = json.loads(aliases_path.read_text(encoding="utf-8"))
            former = set(existing.get("former_slugs", []))
            former.add(src.slug)
            existing["former_slugs"] = sorted(former)
            await asyncio.to_thread(aliases_path.write_text, json.dumps(existing, indent=2), "utf-8")
            rel_alias = aliases_path.relative_to(dst_archive.repo_root).as_posix()
            await _archive_commit(dst_archive.repo, settings, f"adopt: record alias for {src.slug}", [rel_alias])
        except Exception as exc:
            console.print(f"[yellow]Warning: failed to write aliases.json: {exc}[/]")

    try:
        asyncio.run(_apply())
        console.print("[green]Adoption apply completed.[/]")
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc


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
