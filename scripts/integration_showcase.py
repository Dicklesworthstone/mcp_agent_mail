#!/usr/bin/env python3
"""
Extremely verbose, standalone integration walk-through for mcp-agent-mail.

This script does **not** rely on pytest. Instead, it spins up an in-memory FastMCP
server instance and drives realistic multi-agent workflows across multiple projects.
The output leans heavily on Rich so humans (or supervising agents) can follow every
step: environment bootstrap, project setup, identity registration, file reservations, contact
handshakes, messaging, acknowledgements, and search/summarisation flows.

Usage
-----
uv run python scripts/integration_showcase.py

Nothing here performs destructive actions on your repo. It writes its working data
into a unique temporary directory (printed at startup) so you can inspect the Git /
SQLite artifacts afterwards if desired.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, List, Optional

from fastmcp import Client
from fastmcp.exceptions import ToolError
from rich import box
from rich.align import Align
from rich.console import Console
from rich.json import JSON
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.traceback import install as install_rich_traceback
from rich.tree import Tree

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import reset_database_state

install_rich_traceback(show_locals=False)

console = Console()


# --------------------------------------------------------------------------- #
# Helper data models
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ProjectHandle:
    """Keeps track of project metadata across tool calls."""

    label: str
    human_key: str
    slug: Optional[str] = None
    description: str = ""


@dataclass(slots=True)
class AgentHandle:
    """Stores agent identity and intent information."""

    codename: str
    program: str
    model: str
    task: str
    project: ProjectHandle
    attachments_policy: str = "auto"
    record: dict[str, Any] | None = None


@dataclass(slots=True)
class MessageEvent:
    """Captures a delivered message for later summary."""

    message_id: int
    project: str
    thread_id: Optional[str]
    subject: str
    sender: str
    recipients: List[str]
    importance: str
    ack_required: bool
    created: str
    summary: str = ""


# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #


def _json_dump(data: Any) -> str:
    """Convertible JSON dump with support for datetimes and Paths."""

    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Path):
            return obj.as_posix()
        if isinstance(obj, set):
            return sorted(default(item) for item in obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            return {k: default(v) for k, v in vars(obj).items()}
        return str(obj)

    return json.dumps(data, indent=2, sort_keys=True, default=default)


def _syntax_blob(payload: Any, lexer: str = "json") -> Syntax:
    """Create a Rich syntax block for pretty-printing payloads."""
    return Syntax(_json_dump(payload), lexer, theme="monokai", word_wrap=True)


def _section(title: str, subtitle: str | None = None, border_style: str = "bright_cyan") -> Panel:
    """Return a stylised panel for major sections."""
    return Panel.fit(
        Align.center(title, vertical="middle"),
        title=subtitle,
        border_style=border_style,
    )


def _build_environment() -> dict[str, Any]:
    """Prepare a fresh directory for the integration run and configure env vars."""
    base_dir = Path(mkdtemp(prefix="mcp_agent_mail_integration_")).resolve()
    storage_root = base_dir / "storage"
    repos_root = base_dir / "repos"
    db_path = base_dir / "integration.sqlite3"
    for path in (storage_root, repos_root):
        path.mkdir(parents=True, exist_ok=True)

    env_overrides = {
        "APP_ENVIRONMENT": "integration",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "HTTP_HOST": "127.0.0.1",
        "HTTP_PORT": "8765",
        "HTTP_PATH": "/api/",
        "STORAGE_ROOT": str(storage_root),
        "GIT_AUTHOR_NAME": "integration-bot",
        "GIT_AUTHOR_EMAIL": "integration-bot@example.com",
        "INLINE_IMAGE_MAX_BYTES": str(128 * 1024),
        "CONVERT_IMAGES": "true",
        "KEEP_ORIGINAL_IMAGES": "false",
        "HTTP_RBAC_ENABLED": "false",
        "LLM_ENABLED": "false",
        "FILE_RESERVATIONS_ENFORCEMENT_ENABLED": "true",
        "CONTACT_ENFORCEMENT_ENABLED": "false",
        "TOOL_METRICS_EMIT_ENABLED": "false",
    }

    reset_database_state()
    clear_settings_cache()
    for key, value in env_overrides.items():
        os.environ[key] = value
    clear_settings_cache()

    return {
        "base_dir": base_dir,
        "storage_root": storage_root,
        "repos_root": repos_root,
        "db_path": db_path,
        "env": env_overrides,
    }


class Stepper:
    """Manages numbered steps and consistent logging furniture."""

    def __init__(self, *, console: Console, client: Client):
        self.console = console
        self.client = client
        self.step_index = 0
        self.events: list[MessageEvent] = []

    async def call_tool(
        self,
        *,
        actor: str,
        project_label: str,
        tool: str,
        description: str,
        arguments: dict[str, Any],
        highlight_language: str = "json",
    ):
        self.step_index += 1
        header = Table.grid(padding=(0, 1))
        header.add_column(justify="right", style="bold bright_cyan")
        header.add_column(style="bold white")
        header.add_row("Step", f"{self.step_index:02d}")
        header.add_row("Actor", actor)
        header.add_row("Project", project_label)
        header.add_row("Tool", tool)
        header.add_row("Intent", description)

        self.console.print(Panel(header, title="Executing", border_style="bright_magenta"))
        self.console.print(Panel(_syntax_blob(arguments, lexer=highlight_language), title="Payload →", border_style="cyan"))

        try:
            result = await self.client.call_tool(tool, arguments)
        except ToolError as exc:
            self.console.print(Panel.fit(str(exc), title="Tool Failure", border_style="red"))
            raise

        structured = result.structured_content or {}
        renderable = JSON.from_data(structured) if structured else _syntax_blob(result.data or {})
        self.console.print(Panel(renderable, title="Result ←", border_style="green"))

        return result

    def capture_message(
        self,
        *,
        payload: dict[str, Any],
        project: str,
        summary: str = "",
    ) -> None:
        created = (
            payload.get("created_ts")
            or payload.get("created")
            or payload.get("timestamp")
            or ""
        )
        self.events.append(
            MessageEvent(
                message_id=int(payload["id"]),
                project=project,
                thread_id=payload.get("thread_id"),
                subject=payload.get("subject", "<no subject>"),
                sender=payload.get("from", payload.get("sender", "?")),
                recipients=list(payload.get("to", [])),
                importance=payload.get("importance", "normal"),
                ack_required=bool(payload.get("ack_required")),
                created=str(created),
                summary=summary,
            )
        )

    def render_timeline(self) -> None:
        if not self.events:
            return
        tree = Tree("📬 Threads observed during the run", guide_style="bright_blue")
        threads: dict[str, list[MessageEvent]] = {}
        for event in self.events:
            key = event.thread_id or f"msg-{event.message_id}"
            threads.setdefault(key, []).append(event)
        for thread_id, items in sorted(threads.items()):
            branch = tree.add(f"[bold yellow]{thread_id}[/] ({len(items)} message{'s' if len(items)!=1 else ''})")
            for item in items:
                recipients = ", ".join(item.recipients) or "—"
                timestamp = item.created or "unknown time"
                meta = f"[dim]{timestamp}[/] • [cyan]{item.project}[/] • [green]{item.sender}[/] → [white]{recipients}[/]"
                detail = f"{meta} • {item.subject}"
                if item.summary:
                    detail += f"\n    [dim]{item.summary}[/]"
                branch.add(detail)
        self.console.print(tree)


# --------------------------------------------------------------------------- #
# Main scenario
# --------------------------------------------------------------------------- #


async def run() -> None:
    env_info = _build_environment()

    console.print(Rule("[b]mcp-agent-mail Integration Showcase[/b]", style="bright_cyan"))
    console.print(
        Panel(
            Markdown(
                """\
**Goal:** Demonstrate end-to-end agent collaboration spanning
- a shared backend repo (co-edit file reservation flow),
- a sibling frontend repo (cross-project contact & messaging),
- and an unrelated data project (isolation sanity check).

Every tool call is narrated before execution, inputs/outputs are syntax highlighted, and a message timeline is produced at the end."""
            ),
            border_style="cyan",
        )
    )

    environment_table = Table(title="Ephemeral Environment", box=box.ROUNDED, show_header=False, padding=(0, 1))
    environment_table.add_row("Base directory", str(env_info["base_dir"]))
    environment_table.add_row("SQLite path", str(env_info["db_path"]))
    environment_table.add_row("Archive root", str(env_info["storage_root"]))
    environment_table.add_row("Repo staging root", str(env_info["repos_root"]))
    console.print(environment_table)
    console.print("[dim]Tip: cleanup the directory above when you're finished inspecting artifacts.[/dim]\n")

    # Create placeholder repo paths (purely for realism when using absolute human_keys).
    backend_repo = env_info["repos_root"] / "photonstack-backend"
    frontend_repo = env_info["repos_root"] / "photonstack-frontend"
    data_repo = env_info["repos_root"] / "lumen-data"
    for path in (backend_repo, frontend_repo, data_repo):
        path.mkdir(parents=True, exist_ok=True)

    projects = {
        "backend": ProjectHandle(
            label="PhotonStack Backend",
            human_key=str(backend_repo),
            description="Primary FastAPI service powering PhotonStack's APIs.",
        ),
        "frontend": ProjectHandle(
            label="PhotonStack Frontend",
            human_key=str(frontend_repo),
            description="Next.js UI layer for PhotonStack. Shares coordination with backend agents.",
        ),
        "data": ProjectHandle(
            label="Lumen Data Warehouse",
            human_key=str(data_repo),
            description="Independent analytics project with no crossover to PhotonStack.",
        ),
    }

    agents = {
        "blue": AgentHandle(
            codename="BlueLake",
            program="codex-cli",
            model="gpt-5-codex",
            task="Guardian for backend auth refactor",
            project=projects["backend"],
            attachments_policy="inline",
        ),
        "green": AgentHandle(
            codename="GreenStone",
            program="codex-cli",
            model="gpt-5-codex",
            task="Implements payment webhooks in backend",
            project=projects["backend"],
        ),
        "orange": AgentHandle(
            codename="OrangeHill",
            program="claude-code",
            model="opus-4.1",
            task="Frontend navigation overhaul",
            project=projects["frontend"],
        ),
        "purple": AgentHandle(
            codename="PurpleBear",
            program="gemini-cli",
            model="gemini-2.5-pro",
            task="Frontend accessibility QA",
            project=projects["frontend"],
        ),
        "black": AgentHandle(
            codename="BlackSnow",
            program="open-code",
            model="atlas-1",
            task="ETL reliability sweeps",
            project=projects["data"],
        ),
    }

    server = build_mcp_server()
    settings = get_settings()

    settings_table = Table(title="Active Settings Snapshot", box=box.SIMPLE_HEAVY, padding=(0, 1))
    settings_table.add_column("Key", style="cyan", justify="right")
    settings_table.add_column("Value", style="white")
    settings_table.add_row("Environment", settings.environment)
    settings_table.add_row("HTTP Endpoint", f"http://{settings.http.host}:{settings.http.port}{settings.http.path}")
    settings_table.add_row("Database URL", settings.database.url)
    settings_table.add_row("Storage Root", settings.storage.root)
    settings_table.add_row("Images → WebP", str(settings.storage.convert_images))
    settings_table.add_row("File reservations enforcement", str(settings.file_reservations_enforcement_enabled))
    console.print(settings_table)
    console.print()

    async with Client(server) as client:
        stepper = Stepper(console=console, client=client)

        # ------------------------------------------------------------------ #
        # Bootstrap health & projects
        # ------------------------------------------------------------------ #
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Bootstrapping MCP server", total=None)
            await stepper.call_tool(
                actor="Supervisor",
                project_label="N/A",
                tool="health_check",
                description="Ensure the coordination server is ready before orchestrating agents.",
                arguments={},
            )
            progress.update(task, description="Declaring PhotonStack & Lumen projects")
            for handle in projects.values():
                result = await stepper.call_tool(
                    actor="Supervisor",
                    project_label=handle.label,
                    tool="ensure_project",
                    description=f"Create or verify project archive for {handle.label}.",
                    arguments={"human_key": handle.human_key},
                )
                handle.slug = result.data["slug"]
            progress.update(task, completed=True)

        # ------------------------------------------------------------------ #
        # Register all agents
        # ------------------------------------------------------------------ #
        console.print(Rule("Registering agent identities", style="green"))
        for agent in agents.values():
            response = await stepper.call_tool(
                actor=agent.codename,
                project_label=agent.project.label,
                tool="create_agent_identity",
                description="Provision a fresh profile with attachments policy and task context.",
                arguments={
                    "project_key": agent.project.human_key,
                    "program": agent.program,
                    "model": agent.model,
                    "name_hint": agent.codename,
                    "task_description": agent.task,
                    "attachments_policy": agent.attachments_policy,
                },
            )
            agent.record = response.data
            whois = await stepper.call_tool(
                actor=agent.codename,
                project_label=agent.project.label,
                tool="whois",
                description="Inspect the freshly created profile and recent commits (should be empty for new agent).",
                arguments={
                    "project_key": agent.project.human_key,
                    "agent_name": agent.codename,
                    "include_recent_commits": True,
                },
            )
            console.print(Panel(JSON.from_data(whois.structured_content or {}), border_style="blue"))

        # ------------------------------------------------------------------ #
        # Backend repo collaboration (shared project)
        # ------------------------------------------------------------------ #
        console.print(Rule("Backend collaboration: coordinating file reservations & messaging", style="yellow"))

        reservation_result = await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="file_reservation_paths",
            description="BlueLake stakes an exclusive file reservation on backend auth routes.",
            arguments={
                "project_key": projects["backend"].human_key,
                "agent_name": agents["blue"].codename,
                "paths": ["services/backend/auth/*.py"],
                "ttl_seconds": 2 * 3600,
                "exclusive": True,
                "reason": "Implementing session token hardening.",
            },
        )
        reservation_payload = reservation_result.data
        console.print(Panel(_syntax_blob(reservation_payload), title="Active file reservation ledger", border_style="yellow"))

        conflict_attempt = await stepper.call_tool(
            actor=agents["green"].codename,
            project_label=projects["backend"].label,
            tool="file_reservation_paths",
            description="GreenStone attempts to reserve the same files to illustrate conflict detection.",
            arguments={
                "project_key": projects["backend"].human_key,
                "agent_name": agents["green"].codename,
                "paths": ["services/backend/auth/*.py"],
                "ttl_seconds": 3600,
            },
        )
        console.print(Panel(_syntax_blob(conflict_attempt.data), title="Conflict response", border_style="red"))

        # GreenStone picks a different surface.
        await stepper.call_tool(
            actor=agents["green"].codename,
            project_label=projects["backend"].label,
            tool="file_reservation_paths",
            description="GreenStone pivots to payment webhook files to avoid conflict.",
            arguments={
                "project_key": projects["backend"].human_key,
                "agent_name": agents["green"].codename,
                "paths": ["services/backend/payments/*.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": "Shipping Stripe webhook handlers.",
            },
        )

        backend_message = await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="send_message",
            description="BlueLake shares a refactor plan with GreenStone, requiring acknowledgement.",
            arguments={
                "project_key": projects["backend"].human_key,
                "sender_name": agents["blue"].codename,
                "to": [agents["green"].codename],
                "subject": "Auth hardening rollout plan",
                "body_md": (
                    "## Backend Auth Refactor\n"
                    "- rotate signing keys per environment\n"
                    "- migrate session storage to Redis sentinel\n"
                    "- add regression suite seeded from `/tests/auth/*.yaml`\n\n"
                    "Please acknowledge once you've synced the new fixtures."
                ),
                "importance": "high",
                "ack_required": True,
                "thread_id": "backend-auth-hardening",
            },
        )

        deliveries = backend_message.data.get("deliveries", [])
        for delivery in deliveries:
            stepper.capture_message(
                payload=delivery["payload"],
                project=delivery["project"],
                summary="Auth refactor kickoff with explicit ack.",
            )

        inbox_green = await stepper.call_tool(
            actor=agents["green"].codename,
            project_label=projects["backend"].label,
            tool="fetch_inbox",
            description="GreenStone fetches inbox to read BlueLake's instructions.",
            arguments={
                "project_key": projects["backend"].human_key,
                "agent_name": agents["green"].codename,
                "include_bodies": True,
                "limit": 5,
            },
        )
        inbox_data = inbox_green.structured_content.get("result", [])
        if inbox_data:
            first_msg = inbox_data[0]
            await stepper.call_tool(
                actor=agents["green"].codename,
                project_label=projects["backend"].label,
                tool="acknowledge_message",
                description="GreenStone acknowledges the refactor plan.",
                arguments={
                    "project_key": projects["backend"].human_key,
                    "agent_name": agents["green"].codename,
                    "message_id": first_msg["id"],
                },
            )

            reply = await stepper.call_tool(
                actor=agents["green"].codename,
                project_label=projects["backend"].label,
                tool="reply_message",
                description="GreenStone confirms readiness and shares test coverage notes.",
                arguments={
                    "project_key": projects["backend"].human_key,
                    "message_id": first_msg["id"],
                    "sender_name": agents["green"].codename,
                    "body_md": (
                        "Synced ✅ — I'll extend the webhook regression cases while you handle key rotation. "
                        "Ping if you need Stripe fixtures regenerated."
                    ),
                },
            )
            stepper.capture_message(payload=reply.data, project=projects["backend"].label, summary="GreenStone confirms task ownership.")

        # ------------------------------------------------------------------ #
        # Cross-project contact (backend ↔ frontend)
        # ------------------------------------------------------------------ #
        console.print(Rule("Cross-project contact handshake", style="magenta"))

        await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="request_contact",
            description="BlueLake requests permission to coordinate with OrangeHill in the frontend repo.",
            arguments={
                "project_key": projects["backend"].human_key,
                "from_agent": agents["blue"].codename,
                "to_agent": agents["orange"].codename,
                "to_project": projects["frontend"].human_key,
                "reason": "Need to align auth UI changes with backend token rollout.",
            },
        )
        stepper.capture_message(
            payload={
                "id": int(datetime.now().timestamp()),
                "from": agents["blue"].codename,
                "to": [agents["orange"].codename],
                "subject": "Contact request",
                "thread_id": None,
                "importance": "normal",
                "ack_required": True,
                "created": datetime.now().isoformat(),
            },
            project=projects["frontend"].label,
            summary="Automated contact request notification.",
        )

        await stepper.call_tool(
            actor=agents["orange"].codename,
            project_label=projects["frontend"].label,
            tool="fetch_inbox",
            description="OrangeHill checks inbox to review the contact request.",
            arguments={
                "project_key": projects["frontend"].human_key,
                "agent_name": agents["orange"].codename,
                "include_bodies": True,
                "limit": 5,
            },
        )

        await stepper.call_tool(
            actor=agents["orange"].codename,
            project_label=projects["frontend"].label,
            tool="respond_contact",
            description="OrangeHill approves the contact request, allowing backend ↔ frontend messages.",
            arguments={
                "project_key": projects["frontend"].human_key,
                "to_agent": agents["orange"].codename,
                "from_agent": agents["blue"].codename,
                "from_project": projects["backend"].human_key,
                "accept": True,
                "ttl_seconds": 7 * 24 * 3600,
            },
        )

        await stepper.call_tool(
            actor=agents["orange"].codename,
            project_label=projects["frontend"].label,
            tool="request_contact",
            description="OrangeHill reciprocates by requesting direct contact back to the backend project.",
            arguments={
                "project_key": projects["frontend"].human_key,
                "from_agent": agents["orange"].codename,
                "to_agent": agents["blue"].codename,
                "to_project": projects["backend"].human_key,
                "reason": "Need backend API status updates while iterating on login UI.",
            },
        )

        await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="respond_contact",
            description="BlueLake approves the reciprocal contact request.",
            arguments={
                "project_key": projects["backend"].human_key,
                "to_agent": agents["blue"].codename,
                "from_agent": agents["orange"].codename,
                "from_project": projects["frontend"].human_key,
                "accept": True,
                "ttl_seconds": 7 * 24 * 3600,
            },
        )

        await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="list_contacts",
            description="BlueLake audits approved outbound contacts.",
            arguments={
                "project_key": projects["backend"].human_key,
                "agent_name": agents["blue"].codename,
            },
        )

        cross_project = await stepper.call_tool(
            actor=agents["blue"].codename,
            project_label=projects["backend"].label,
            tool="send_message",
            description="BlueLake briefs OrangeHill on backend auth token UX implications.",
            arguments={
                "project_key": projects["backend"].human_key,
                "sender_name": agents["blue"].codename,
                "to": [f"project:{projects['frontend'].slug}#{agents['orange'].codename}"],
                "subject": "Backend auth changes affecting login UI",
                "body_md": (
                    "Heads-up on the token refresh changes:\n"
                    "- refresh window extended to 12h (update UI copy)\n"
                    "- new `/session/refresh` endpoint returning `expires_in`\n"
                    "- fallback MFA dialog should surface backend error codes verbatim\n\n"
                    "Let's target a coordinated release Thursday 10:00 UTC."
                ),
                "importance": "normal",
                "thread_id": "auth-ui-sync",
            },
        )
        for delivery in cross_project.data.get("deliveries", []):
            stepper.capture_message(
                payload=delivery["payload"],
                project=delivery["project"],
                summary="Backend → Frontend coordination note.",
            )

        orange_inbox_post = await stepper.call_tool(
            actor=agents["orange"].codename,
            project_label=projects["frontend"].label,
            tool="fetch_inbox",
            description="OrangeHill receives the cross-project message after contact approval.",
            arguments={
                "project_key": projects["frontend"].human_key,
                "agent_name": agents["orange"].codename,
                "include_bodies": True,
                "limit": 5,
            },
        )
        thread_messages = orange_inbox_post.structured_content.get("result", [])
        if thread_messages:
            target_msg = thread_messages[0]
            reply_frontend = await stepper.call_tool(
                actor=agents["orange"].codename,
                project_label=projects["frontend"].label,
                tool="reply_message",
                description="OrangeHill replies from the frontend project back to BlueLake.",
                arguments={
                    "project_key": projects["frontend"].human_key,
                    "message_id": target_msg["id"],
                    "sender_name": agents["orange"].codename,
                    "body_md": "Copy that — I'll push MFA copy updates and flag QA for Thursday AM.",
                    "to": [f"project:{projects['backend'].slug}#{agents['blue'].codename}"],
                },
            )
            stepper.capture_message(
                payload=reply_frontend.data,
                project=projects["frontend"].label,
                summary="Frontend acknowledges auth change timeline.",
            )

        # ------------------------------------------------------------------ #
        # Unrelated project sanity check
        # ------------------------------------------------------------------ #
        console.print(Rule("Isolated project sanity check", style="bright_blue"))
        await stepper.call_tool(
            actor=agents["black"].codename,
            project_label=projects["data"].label,
            tool="send_message",
            description="BlackSnow publishes a standalone data pipeline status email (should not leak elsewhere).",
            arguments={
                "project_key": projects["data"].human_key,
                "sender_name": agents["black"].codename,
                "to": [agents["black"].codename],
                "subject": "Daily ETL digest",
                "body_md": (
                    "- ✅ warehouse load completed in 12m (SLA 15m)\n"
                    "- ⚠️ 2 delayed events: investigating source `ingest-api`\n"
                    "- 📊 dashboards refreshed @ 08:05 UTC\n"
                ),
                "thread_id": "daily-etl-digest",
            },
        )

        await stepper.call_tool(
            actor=agents["black"].codename,
            project_label=projects["data"].label,
            tool="fetch_inbox",
            description="Confirm the ETL digest is accessible to the data-only agent.",
            arguments={
                "project_key": projects["data"].human_key,
                "agent_name": agents["black"].codename,
                "limit": 3,
            },
        )

        # ------------------------------------------------------------------ #
        # Search & summaries
        # ------------------------------------------------------------------ #
        console.print(Rule("Knowledge retrieval", style="bright_green"))
        await stepper.call_tool(
            actor="Supervisor",
            project_label=projects["backend"].label,
            tool="search_messages",
            description="Search backend messages for references to 'MFA' across threads.",
            arguments={
                "project_key": projects["backend"].human_key,
                "query": "MFA",
                "limit": 5,
            },
        )

        await stepper.call_tool(
            actor="Supervisor",
            project_label=projects["backend"].label,
            tool="summarize_thread",
            description="Generate a synopsis of the backend auth hardening thread.",
            arguments={
                "project_key": projects["backend"].human_key,
                "thread_id": "backend-auth-hardening",
                "include_examples": True,
            },
        )

        # ------------------------------------------------------------------ #
        # Recap timeline
        # ------------------------------------------------------------------ #
        console.print(Rule("Timeline recap", style="white"))
        stepper.render_timeline()

        recap_layout = Layout(name="recap")
        recap_layout.split_column(
            Layout(name="key_takeaways", ratio=2),
            Layout(name="next_steps", ratio=1),
        )
        recap_layout["key_takeaways"].update(
            Panel(
                Markdown(
                    """\
### Key Observations
- File reservations prevented overlapping backend work without blocking alternative surfaces.
- Contact handshake enabled cross-project routing with clear audit trail.
- Message acknowledgements and replies stayed scoped to their originating projects.
- Search & summaries surfaced the negotiated plan for downstream agents."""
                ),
                border_style="bright_white",
            )
        )
        recap_layout["next_steps"].update(
            Panel(
                Markdown(
                    """\
### Suggested Follow-ups
- Review the Git commits under the printed storage root for human verification.
- Run `uv run python -m mcp_agent_mail.http` to expose the same flows over HTTP.
- Integrate this script into your CI smoke suite for regression coverage."""
                ),
                border_style="bright_white",
            )
        )
        with Live(recap_layout, refresh_per_second=2, transient=True):
            await asyncio.sleep(0.5)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[red]Interrupted by user.[/red]")


if __name__ == "__main__":
    main()
