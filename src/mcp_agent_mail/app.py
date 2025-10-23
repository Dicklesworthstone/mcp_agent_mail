"""Application factory for the MCP Agent Mail server."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import textwrap
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Optional, cast

from fastmcp import Context, FastMCP
from sqlalchemy import asc, desc, func, or_, select, text, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import aliased

from .config import Settings, get_settings
from .db import ensure_schema, get_session, init_engine
from .models import Agent, Claim, Message, MessageRecipient, Project
from .storage import (
    AsyncFileLock,
    ProjectArchive,
    ensure_archive,
    process_attachments,
    write_agent_profile,
    write_claim_record,
    write_message_bundle,
)
from .utils import generate_agent_name, sanitize_agent_name, slugify


def _lifespan_factory(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastMCP):
        init_engine(settings)
        await ensure_schema(settings)
        yield

    return lifespan


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _project_to_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "slug": project.slug,
        "human_key": project.human_key,
        "created_at": _iso(project.created_at),
    }


def _agent_to_dict(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "program": agent.program,
        "model": agent.model,
        "task_description": agent.task_description,
        "inception_ts": _iso(agent.inception_ts),
        "last_active_ts": _iso(agent.last_active_ts),
        "project_id": agent.project_id,
    }


def _message_to_dict(message: Message, include_body: bool = True) -> dict[str, Any]:
    data = {
        "id": message.id,
        "project_id": message.project_id,
        "sender_id": message.sender_id,
        "thread_id": message.thread_id,
        "subject": message.subject,
        "importance": message.importance,
        "ack_required": message.ack_required,
        "created_ts": _iso(message.created_ts),
        "attachments": message.attachments,
    }
    if include_body:
        data["body_md"] = message.body_md
    return data


def _message_frontmatter(
    message: Message,
    project: Project,
    sender: Agent,
    to_agents: Sequence[Agent],
    cc_agents: Sequence[Agent],
    bcc_agents: Sequence[Agent],
    attachments: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "project": project.human_key,
        "project_slug": project.slug,
        "from": sender.name,
        "to": [agent.name for agent in to_agents],
        "cc": [agent.name for agent in cc_agents],
        "bcc": [agent.name for agent in bcc_agents],
        "subject": message.subject,
        "importance": message.importance,
        "ack_required": message.ack_required,
        "created": _iso(message.created_ts),
        "attachments": attachments,
    }


async def _ensure_project(human_key: str) -> Project:
    await ensure_schema()
    slug = slugify(human_key)
    async with get_session() as session:
        result = await session.execute(select(Project).where(Project.slug == slug))
        project = result.scalars().first()
        if project:
            return project
        project = Project(slug=slug, human_key=human_key)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project


async def _get_project_by_identifier(identifier: str) -> Project:
    await ensure_schema()
    slug = slugify(identifier)
    async with get_session() as session:
        result = await session.execute(select(Project).where(Project.slug == slug))
        project = result.scalars().first()
        if not project:
            raise NoResultFound(f"Project '{identifier}' not found.")
        return project


async def _agent_name_exists(project: Project, name: str) -> bool:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")
    async with get_session() as session:
        result = await session.execute(
            select(Agent.id).where(Agent.project_id == project.id, func.lower(Agent.name) == name.lower())
        )
        return result.first() is not None


async def _generate_unique_agent_name(
    project: Project,
    settings: Settings,
    name_hint: Optional[str] = None,
) -> str:
    archive = await ensure_archive(settings, project.slug)

    async def available(candidate: str) -> bool:
        return not await _agent_name_exists(project, candidate) and not (archive.root / "agents" / candidate).exists()

    if name_hint:
        sanitized = sanitize_agent_name(name_hint)
        if not sanitized:
            raise ValueError("Name hint must contain alphanumeric characters.")
        if not await available(sanitized):
            raise ValueError(f"Agent name '{sanitized}' is already in use.")
        return sanitized

    for _ in range(1024):
        candidate = sanitize_agent_name(generate_agent_name())
        if candidate and await available(candidate):
            return candidate
    raise RuntimeError("Unable to generate a unique agent name.")


async def _create_agent_record(
    project: Project,
    name: str,
    program: str,
    model: str,
    task_description: str,
) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before creating agents.")
    await ensure_schema()
    async with get_session() as session:
        agent = Agent(
            project_id=project.id,
            name=name,
            program=program,
            model=model,
            task_description=task_description,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


async def _get_or_create_agent(
    project: Project,
    name: Optional[str],
    program: str,
    model: str,
    task_description: str,
    settings: Settings,
) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before creating agents.")
    if name is None:
        desired_name = await _generate_unique_agent_name(project, settings, None)
    else:
        sanitized = sanitize_agent_name(name)
        if not sanitized:
            raise ValueError("Agent name must contain alphanumeric characters.")
        desired_name = sanitized
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, Agent.name == desired_name)
        )
        agent = result.scalars().first()
        if agent:
            agent.program = program
            agent.model = model
            agent.task_description = task_description
            agent.last_active_ts = datetime.now(timezone.utc)
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
        else:
            agent = Agent(
                project_id=project.id,
                name=desired_name,
                program=program,
                model=model,
                task_description=task_description,
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
    archive = await ensure_archive(settings, project.slug)
    async with AsyncFileLock(archive.lock_path):
        await write_agent_profile(archive, _agent_to_dict(agent))
    return agent


async def _get_agent(project: Project, name: str) -> Agent:
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name) == name.lower())
        )
        agent = result.scalars().first()
        if not agent:
            raise NoResultFound(f"Agent '{name}' not registered for project '{project.human_key}'.")
        return agent


async def _create_message(
    project: Project,
    sender: Agent,
    subject: str,
    body_md: str,
    recipients: Sequence[tuple[Agent, str]],
    importance: str,
    ack_required: bool,
    thread_id: Optional[str],
    attachments: Sequence[dict[str, Any]],
) -> Message:
    if project.id is None:
        raise ValueError("Project must have an id before creating messages.")
    if sender.id is None:
        raise ValueError("Sender must have an id before sending messages.")
    await ensure_schema()
    async with get_session() as session:
        message = Message(
            project_id=project.id,
            sender_id=sender.id,
            subject=subject,
            body_md=body_md,
            importance=importance,
            ack_required=ack_required,
            thread_id=thread_id,
            attachments=list(attachments),
        )
        session.add(message)
        await session.flush()
        for recipient, kind in recipients:
            entry = MessageRecipient(message_id=message.id, agent_id=recipient.id, kind=kind)
            session.add(entry)
        sender.last_active_ts = datetime.now(timezone.utc)
        session.add(sender)
        await session.commit()
        await session.refresh(message)
    return message


async def _create_claim(
    project: Project,
    agent: Agent,
    path: str,
    exclusive: bool,
    reason: str,
    ttl_seconds: int,
) -> Claim:
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before creating claims.")
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    await ensure_schema()
    async with get_session() as session:
        claim = Claim(
            project_id=project.id,
            agent_id=agent.id,
            path_pattern=path,
            exclusive=exclusive,
            reason=reason,
            expires_ts=expires,
        )
        session.add(claim)
        await session.commit()
        await session.refresh(claim)
    return claim


async def _expire_stale_claims(project_id: int) -> None:
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        await session.execute(
            update(Claim)
            .where(
                Claim.project_id == project_id,
                cast(Any, Claim.released_ts).is_(None),
                Claim.expires_ts < now,
            )
            .values(released_ts=now)
        )
        await session.commit()


def _claims_conflict(existing: Claim, candidate_path: str, candidate_exclusive: bool, candidate_agent: Agent) -> bool:
    if existing.released_ts is not None:
        return False
    if existing.agent_id == candidate_agent.id:
        return False
    if not existing.exclusive and not candidate_exclusive:
        return False
    normalized_existing = existing.path_pattern
    return (
        fnmatch.fnmatchcase(candidate_path, normalized_existing)
        or fnmatch.fnmatchcase(normalized_existing, candidate_path)
        or normalized_existing == candidate_path
    )


async def _list_inbox(
    project: Project,
    agent: Agent,
    limit: int,
    urgent_only: bool,
    include_bodies: bool,
    since_ts: Optional[str],
) -> list[dict[str, Any]]:
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before listing inbox.")
    sender_alias = aliased(Agent)
    await ensure_schema()
    async with get_session() as session:
        stmt = (
            select(Message, MessageRecipient.kind, sender_alias.name)
            .join(MessageRecipient, MessageRecipient.message_id == Message.id)
            .join(sender_alias, Message.sender_id == sender_alias.id)
            .where(
                Message.project_id == project.id,
                MessageRecipient.agent_id == agent.id,
            )
            .order_by(desc(Message.created_ts))
            .limit(limit)
        )
        if urgent_only:
            stmt = stmt.where(cast(Any, Message.importance).in_(["high", "urgent"]))
        if since_ts:
            try:
                since_dt = datetime.fromisoformat(since_ts)
            except ValueError:
                since_dt = None
            if since_dt:
                stmt = stmt.where(Message.created_ts > since_dt)
        result = await session.execute(stmt)
        rows = result.all()
    messages: list[dict[str, Any]] = []
    for message, recipient_kind, sender_name in rows:
        payload = _message_to_dict(message, include_body=include_bodies)
        payload["from"] = sender_name
        payload["kind"] = recipient_kind
        messages.append(payload)
    return messages


def _summarize_messages(messages: Sequence[tuple[Message, str]]) -> dict[str, Any]:
    participants: set[str] = set()
    key_points: list[str] = []
    action_items: list[str] = []
    keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")
    for message, sender_name in messages:
        participants.add(sender_name)
        for line in message.body_md.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(('-', '*', '+')) or stripped[:2] in {"1.", "2.", "3."}:
                key_points.append(stripped.lstrip("-+* "))
            upper = stripped.upper()
            if any(token in upper for token in keywords):
                action_items.append(stripped)
    return {
        "participants": sorted(participants),
        "key_points": key_points[:10],
        "action_items": action_items[:10],
        "total_messages": len(messages),
    }


async def _get_message(project: Project, message_id: int) -> Message:
    if project.id is None:
        raise ValueError("Project must have an id before reading messages.")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Message).where(Message.project_id == project.id, Message.id == message_id)
        )
        message = result.scalars().first()
        if not message:
            raise NoResultFound(f"Message '{message_id}' not found for project '{project.human_key}'.")
        return message


async def _get_agent_by_id(project: Project, agent_id: int) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, Agent.id == agent_id)
        )
        agent = result.scalars().first()
        if not agent:
            raise NoResultFound(f"Agent id '{agent_id}' not found for project '{project.human_key}'.")
        return agent


async def _update_recipient_timestamp(
    agent: Agent,
    message_id: int,
    field: str,
) -> Optional[datetime]:
    if agent.id is None:
        raise ValueError("Agent must have an id before updating message state.")
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        stmt = (
            update(MessageRecipient)
            .where(MessageRecipient.message_id == message_id, MessageRecipient.agent_id == agent.id)
            .values({field: now})
        )
        result = await session.execute(stmt)
        await session.commit()
    return now if result.rowcount else None


def build_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server instance."""
    settings: Settings = get_settings()
    lifespan = _lifespan_factory(settings)

    instructions = (
        "You are the MCP Agent Mail coordination server. "
        "Provide message routing, coordination tooling, and project context to cooperating agents."
    )

    mcp = FastMCP(name="mcp-agent-mail", instructions=instructions, lifespan=lifespan)

    async def _deliver_message(
        ctx: Context,
        project: Project,
        sender: Agent,
        to_names: Sequence[str],
        cc_names: Sequence[str],
        bcc_names: Sequence[str],
        subject: str,
        body_md: str,
        attachment_paths: Sequence[str] | None,
        convert_images_override: Optional[bool],
        importance: str,
        ack_required: bool,
        thread_id: Optional[str],
    ) -> dict[str, Any]:
        if not to_names and not cc_names and not bcc_names:
            raise ValueError("At least one recipient must be specified.")
        def _unique(items: Sequence[str]) -> list[str]:
            seen: set[str] = set()
            ordered: list[str] = []
            for item in items:
                if item not in seen:
                    seen.add(item)
                    ordered.append(item)
            return ordered

        to_names = _unique(to_names)
        cc_names = _unique(cc_names)
        bcc_names = _unique(bcc_names)
        to_agents = [await _get_agent(project, name) for name in to_names]
        cc_agents = [await _get_agent(project, name) for name in cc_names]
        bcc_agents = [await _get_agent(project, name) for name in bcc_names]
        recipient_records: list[tuple[Agent, str]] = [(agent, "to") for agent in to_agents]
        recipient_records.extend((agent, "cc") for agent in cc_agents)
        recipient_records.extend((agent, "bcc") for agent in bcc_agents)

        archive = await ensure_archive(settings, project.slug)
        convert_markdown = (
            convert_images_override if convert_images_override is not None else settings.storage.convert_images
        )
        async with AsyncFileLock(archive.lock_path):
            processed_body, attachments_meta, attachment_files = await process_attachments(
                archive,
                body_md,
                attachment_paths or [],
                convert_markdown,
            )
            message = await _create_message(
                project,
                sender,
                subject,
                processed_body,
                recipient_records,
                importance,
                ack_required,
                thread_id,
                attachments_meta,
            )
            frontmatter = _message_frontmatter(
                message,
                project,
                sender,
                to_agents,
                cc_agents,
                bcc_agents,
                attachments_meta,
            )
            recipients_for_archive = [agent.name for agent in to_agents + cc_agents + bcc_agents]
            await write_message_bundle(
                archive,
                frontmatter,
                processed_body,
                sender.name,
                recipients_for_archive,
                attachment_files,
            )
        await ctx.info(
            f"Message {message.id} created by {sender.name} (to {', '.join(recipients_for_archive)})"
        )
        payload = _message_to_dict(message)
        payload.update(
            {
                "from": sender.name,
                "to": [agent.name for agent in to_agents],
                "cc": [agent.name for agent in cc_agents],
                "bcc": [agent.name for agent in bcc_agents],
                "attachments": attachments_meta,
            }
        )
        return payload

    @mcp.tool(name="health_check", description="Return basic readiness information for the Agent Mail server.")
    async def health_check(ctx: Context) -> dict[str, Any]:
        """
        Quick readiness probe for agents and orchestrators.

        When to use
        -----------
        - Before starting a workflow, to ensure the coordination server is reachable
          and configured (right environment, host/port, DB wiring).
        - During incident triage to print basic diagnostics to logs via `ctx.info`.

        What it checks vs what it does not
        ----------------------------------
        - Reports current environment and HTTP binding details.
        - Returns the configured database URL (not a live connection test).
        - Does not perform deep dependency health checks or connection attempts.

        Returns
        -------
        dict
            {
              "status": "ok" | "degraded" | "error",
              "environment": str,
              "http_host": str,
              "http_port": int,
              "database_url": str
            }

        Examples
        --------
        JSON-RPC (generic MCP client):
        ```json
        {"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"health_check","arguments":{}}}
        ```

        Typical agent usage (pseudocode):
        - Call `health_check`.
        - If status != ok, sleep/retry with backoff and log `environment`/`http_host`/`http_port`.
        """
        await ctx.info("Running health check.")
        return {
            "status": "ok",
            "environment": settings.environment,
            "http_host": settings.http.host,
            "http_port": settings.http.port,
            "database_url": settings.database.url,
        }

    @mcp.tool(name="ensure_project")
    async def ensure_project(ctx: Context, human_key: str) -> dict[str, Any]:
        """
        Idempotently create or ensure a project exists for the given human key.

        When to use
        -----------
        - First call in a workflow targeting a new repo/path identifier.
        - As a guard before registering agents or sending messages.

        How it works
        ------------
        - Computes a stable slug from `human_key` (lowercased, safe characters) so
          multiple agents can refer to the same project consistently.
        - Ensures DB row exists and that the on-disk archive is initialized
          (e.g., `messages/`, `agents/`, `claims/` directories).

        Parameters
        ----------
        human_key : str
            A stable identifier for a project (often an absolute path to a repo,
            or a canonical slug that multiple agents can share).

        Returns
        -------
        dict
            Minimal project descriptor: { id, slug, human_key, created_at }.

        Examples
        --------
        JSON-RPC:
        ```json
        {
          "jsonrpc": "2.0",
          "id": "2",
          "method": "tools/call",
          "params": {"name": "ensure_project", "arguments": {"human_key": "/abs/path/backend"}}
        }
        ```

        Common mistakes
        ---------------
        - Passing an ephemeral or relative path as `human_key` (prefer absolute,
          stable identifiers to avoid accidental duplication).
        """
        await ctx.info(f"Ensuring project for key '{human_key}'.")
        project = await _ensure_project(human_key)
        await ensure_archive(settings, project.slug)
        return _project_to_dict(project)

    @mcp.tool(name="register_agent")
    async def register_agent(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name: Optional[str] = None,
        task_description: str = "",
    ) -> dict[str, Any]:
        """
        Create or update an agent identity within a project and persist its profile to Git.

        When to use
        -----------
        - At the start of a coding session by any automated agent.
        - To update an existing agent's program/model/task metadata and bump last_active.

        Semantics
        ---------
        - If `name` is omitted, a memorable adjective+noun name is generated.
        - Reusing the same `name` updates the profile (program/model/task) and
          refreshes `last_active_ts`.
        - A `profile.json` file is written under `agents/<Name>/` in the project archive.

        Parameters
        ----------
        project_key : str
            The same human key you passed to `ensure_project` (or equivalent identifier).
        program : str
            The agent program (e.g., "codex-cli", "claude-code").
        model : str
            The underlying model (e.g., "gpt5-codex", "opus-4.1").
        name : Optional[str]
            Desired agent name. If omitted, a memorable adjective+noun name is generated.
            Names are unique per project; passing the same name updates the profile.
        task_description : str
            Short description of current focus (shows up in directory listings).

        Returns
        -------
        dict
            { id, name, program, model, task_description, inception_ts, last_active_ts, project_id }

        Examples
        --------
        Register with generated name:
        ```json
        {"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"register_agent","arguments":{
          "project_key":"/abs/path/backend","program":"codex-cli","model":"gpt5-codex","task_description":"Auth refactor"
        }}}
        ```

        Register with explicit name:
        ```json
        {"jsonrpc":"2.0","id":"4","method":"tools/call","params":{"name":"register_agent","arguments":{
          "project_key":"/abs/path/backend","program":"claude-code","model":"opus-4.1","name":"BlueLake","task_description":"Navbar redesign"
        }}}
        ```

        Pitfalls
        --------
        - Names are case-insensitive unique. If you see "already in use", pick another or omit `name`.
        - Use the same `project_key` consistently across cooperating agents.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_or_create_agent(project, name, program, model, task_description, settings)
        await ctx.info(f"Registered agent '{agent.name}' for project '{project.human_key}'.")
        return _agent_to_dict(agent)

    @mcp.tool(name="create_agent_identity")
    async def create_agent_identity(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name_hint: Optional[str] = None,
        task_description: str = "",
    ) -> dict[str, Any]:
        """
        Create a new, unique agent identity and persist its profile to Git.

        How this differs from `register_agent`
        --------------------------------------
        - Always creates a new identity with a fresh unique name (never updates an existing one).
        - `name_hint`, if provided, is sanitized (alphanumeric only) and must be available,
          otherwise an error is raised. Without a hint, a readable adjective+noun name is generated.

        When to use
        -----------
        - Spawning a brand new worker agent that should not overwrite an existing profile.
        - Temporary task-specific identities (e.g., short-lived refactor assistants).

        Returns
        -------
        dict
            { id, name, program, model, task_description, inception_ts, last_active_ts, project_id }

        Examples
        --------
        With name hint:
        ```json
        {"jsonrpc":"2.0","id":"c1","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/abs/path/backend","program":"codex-cli","model":"gpt5-codex","name_hint":"GreenCastle",
          "task_description":"DB migration spike"
        }}}
        ```

        Let the server generate the name:
        ```json
        {"jsonrpc":"2.0","id":"c2","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/abs/path/backend","program":"claude-code","model":"opus-4.1"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        unique_name = await _generate_unique_agent_name(project, settings, name_hint)
        agent = await _create_agent_record(project, unique_name, program, model, task_description)
        archive = await ensure_archive(settings, project.slug)
        async with AsyncFileLock(archive.lock_path):
            await write_agent_profile(archive, _agent_to_dict(agent))
        await ctx.info(f"Created new agent identity '{agent.name}' for project '{project.human_key}'.")
        return _agent_to_dict(agent)

    @mcp.tool(name="send_message")
    async def send_message(
        ctx: Context,
        project_key: str,
        sender_name: str,
        to: list[str],
        subject: str,
        body_md: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        attachment_paths: Optional[list[str]] = None,
        convert_images: Optional[bool] = None,
        importance: str = "normal",
        ack_required: bool = False,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Send a Markdown message to one or more recipients and persist canonical and mailbox copies to Git.

        What this does
        --------------
        - Stores message (and recipients) in the database; updates sender's activity
        - Writes a canonical `.md` under `messages/YYYY/MM/`
        - Writes sender outbox and per-recipient inbox copies
        - Optionally converts referenced images to WebP and embeds small images inline
        - Supports explicit attachments via `attachment_paths` in addition to inline references

        Parameters
        ----------
        project_key : str
            Project identifier (same used with `ensure_project`/`register_agent`).
        sender_name : str
            Must match an agent registered in the project.
        to : list[str]
            Primary recipients (agent names). At least one of to/cc/bcc must be non-empty.
        subject : str
            Short subject line that will be visible in inbox/outbox and search results.
        body_md : str
            GitHub-Flavored Markdown body. Image references can be file paths or data URIs.
        cc, bcc : Optional[list[str]]
            Additional recipients by name.
        attachment_paths : Optional[list[str]]
            Extra file paths to include as attachments; will be converted to WebP and stored.
        convert_images : Optional[bool]
            Overrides server default for image conversion/inlining. If None, server settings apply.
        importance : str
            One of {"low","normal","high","urgent"} (free form tolerated; used by filters).
        ack_required : bool
            If true, recipients should call `acknowledge_message` after reading.
        thread_id : Optional[str]
            If provided, message will be associated with an existing thread.

        Returns
        -------
        dict
            Message payload with id, timestamps, recipients, attachments, etc.

        Edge cases
        ----------
        - If no recipients are given, the call fails.
        - Unknown recipient names fail fast; register them first.
        - Non-absolute attachment paths are resolved relative to the project archive root.

        Examples
        --------
        1) Simple message:
        ```json
        {"jsonrpc":"2.0","id":"5","method":"tools/call","params":{"name":"send_message","arguments":{
          "project_key":"/abs/path/backend","sender_name":"GreenCastle","to":["BlueLake"],
          "subject":"Plan for /api/users","body_md":"See below."
        }}}
        ```

        2) Inline image (auto-convert to WebP and inline if small):
        ```json
        {"jsonrpc":"2.0","id":"6a","method":"tools/call","params":{"name":"send_message","arguments":{
          "project_key":"/abs/path/backend","sender_name":"GreenCastle","to":["BlueLake"],
          "subject":"Diagram","body_md":"![diagram](docs/flow.png)","convert_images":true
        }}}
        ```

        3) Explicit attachments:
        ```json
        {"jsonrpc":"2.0","id":"6b","method":"tools/call","params":{"name":"send_message","arguments":{
          "project_key":"/abs/path/backend","sender_name":"GreenCastle","to":["BlueLake"],
          "subject":"Screenshots","body_md":"Please review.","attachment_paths":["shots/a.png","shots/b.png"]
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        sender = await _get_agent(project, sender_name)
        payload = await _deliver_message(
            ctx,
            project,
            sender,
            to,
            cc or [],
            bcc or [],
            subject,
            body_md,
            attachment_paths,
            convert_images,
            importance,
            ack_required,
            thread_id,
        )
        return payload

    @mcp.tool(name="reply_message")
    async def reply_message(
        ctx: Context,
        project_key: str,
        message_id: int,
        sender_name: str,
        body_md: str,
        to: Optional[list[str]] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        subject_prefix: str = "Re:",
    ) -> dict[str, Any]:
        """
        Reply to an existing message, preserving or establishing a thread.

        Behavior
        --------
        - Inherits original `importance` and `ack_required` flags
        - `thread_id` is taken from the original message if present; otherwise, the original id is used
        - Subject is prefixed with `subject_prefix` if not already present
        - Defaults `to` to the original sender if not explicitly provided

        Parameters
        ----------
        project_key : str
            Project identifier.
        message_id : int
            The id of the message you are replying to.
        sender_name : str
            Your agent name (must be registered in the project).
        body_md : str
            Reply body in Markdown.
        to, cc, bcc : Optional[list[str]]
            Recipients by agent name. If omitted, `to` defaults to original sender.
        subject_prefix : str
            Prefix to apply (default "Re:"). Case-insensitive idempotent.

        Returns
        -------
        dict
            Message payload including `thread_id` and `reply_to`.

        Examples
        --------
        Minimal reply to original sender:
        ```json
        {"jsonrpc":"2.0","id":"6","method":"tools/call","params":{"name":"reply_message","arguments":{
          "project_key":"/abs/path/backend","message_id":1234,"sender_name":"BlueLake",
          "body_md":"Questions about the migration plan..."
        }}}
        ```

        Reply with explicit recipients and CC:
        ```json
        {"jsonrpc":"2.0","id":"6c","method":"tools/call","params":{"name":"reply_message","arguments":{
          "project_key":"/abs/path/backend","message_id":1234,"sender_name":"BlueLake",
          "body_md":"Looping ops.","to":["GreenCastle"],"cc":["RedCat"],"subject_prefix":"RE:"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        sender = await _get_agent(project, sender_name)
        original = await _get_message(project, message_id)
        original_sender = await _get_agent_by_id(project, original.sender_id)
        thread_key = original.thread_id or str(original.id)
        subject_prefix_clean = subject_prefix.strip()
        base_subject = original.subject
        if subject_prefix_clean and base_subject.lower().startswith(subject_prefix_clean.lower()):
            reply_subject = base_subject
        else:
            reply_subject = f"{subject_prefix_clean} {base_subject}".strip()
        to_names = to or [original_sender.name]
        payload = await _deliver_message(
            ctx,
            project,
            sender,
            to_names,
            cc or [],
            bcc or [],
            reply_subject,
            body_md,
            None,
            None,
            importance=original.importance,
            ack_required=original.ack_required,
            thread_id=thread_key,
        )
        payload["thread_id"] = thread_key
        payload["reply_to"] = message_id
        return payload

    @mcp.tool(name="fetch_inbox")
    async def fetch_inbox(
        ctx: Context,
        project_key: str,
        agent_name: str,
        limit: int = 20,
        urgent_only: bool = False,
        include_bodies: bool = False,
        since_ts: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve recent messages for an agent without mutating read/ack state.

        Filters
        -------
        - `urgent_only`: only messages with importance in {high, urgent}
        - `since_ts`: ISO-8601 timestamp string; messages strictly newer than this are returned
        - `limit`: max number of messages (default 20)
        - `include_bodies`: include full Markdown bodies in the payloads

        Usage patterns
        --------------
        - Poll after each editing step in an agent loop to pick up coordination messages.
        - Use `since_ts` with the timestamp from your last poll for efficient incremental fetches.
        - Combine with `acknowledge_message` if `ack_required` is true.

        Returns
        -------
        list[dict]
            Each message includes: { id, subject, from, created_ts, importance, ack_required, kind, [body_md] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"7","method":"tools/call","params":{"name":"fetch_inbox","arguments":{
          "project_key":"/abs/path/backend","agent_name":"BlueLake","since_ts":"2025-10-23T00:00:00+00:00"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        items = await _list_inbox(project, agent, limit, urgent_only, include_bodies, since_ts)
        await ctx.info(f"Fetched {len(items)} messages for '{agent.name}'. urgent_only={urgent_only}")
        return items

    @mcp.tool(name="mark_message_read")
    async def mark_message_read(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
    ) -> dict[str, Any]:
        """
        Mark a specific message as read for the given agent.

        Notes
        -----
        - Read receipts are per-recipient; this only affects the specified agent.
        - This does not send an acknowledgement; use `acknowledge_message` for that.
        - Safe to call multiple times; later calls return the original timestamp.

        Returns
        -------
        dict
            { message_id, read: bool, read_at: iso8601 | null }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"8","method":"tools/call","params":{"name":"mark_message_read","arguments":{
          "project_key":"/abs/path/backend","agent_name":"BlueLake","message_id":1234
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        await _get_message(project, message_id)
        read_ts = await _update_recipient_timestamp(agent, message_id, "read_ts")
        await ctx.info(f"Marked message {message_id} read for '{agent.name}'.")
        return {"message_id": message_id, "read": bool(read_ts), "read_at": _iso(read_ts) if read_ts else None}

    @mcp.tool(name="acknowledge_message")
    async def acknowledge_message(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
    ) -> dict[str, Any]:
        """
        Acknowledge a message addressed to an agent (and mark as read).

        Behavior
        --------
        - Sets both read_ts and ack_ts for the (agent, message) pairing
        - Safe to call multiple times; subsequent calls will return the prior timestamps

        When to use
        -----------
        - Respond to messages with `ack_required=true` to signal explicit receipt.
        - Agents can treat an acknowledgement as a lightweight, non-textual reply.

        Returns
        -------
        dict
            { message_id, acknowledged: bool, acknowledged_at: iso8601 | null, read_at: iso8601 | null }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"9","method":"tools/call","params":{"name":"acknowledge_message","arguments":{
          "project_key":"/abs/path/backend","agent_name":"BlueLake","message_id":1234
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        await _get_message(project, message_id)
        read_ts = await _update_recipient_timestamp(agent, message_id, "read_ts")
        ack_ts = await _update_recipient_timestamp(agent, message_id, "ack_ts")
        await ctx.info(f"Acknowledged message {message_id} for '{agent.name}'.")
        return {
            "message_id": message_id,
            "acknowledged": bool(ack_ts),
            "acknowledged_at": _iso(ack_ts) if ack_ts else None,
            "read_at": _iso(read_ts) if read_ts else None,
        }

    @mcp.tool(name="search_messages")
    async def search_messages(
        ctx: Context,
        project_key: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Full-text search over subject and body for a project.

        Tips
        ----
        - SQLite FTS5 syntax supported: phrases ("build plan"), prefix (mig*), boolean (plan AND users)
        - Results are ordered by bm25 score (best matches first)
        - Limit defaults to 20; raise for broad queries

        Query examples
        ---------------
        - Phrase search: `"build plan"`
        - Prefix: `migrat*`
        - Boolean: `plan AND users`
        - Require urgent: `urgent AND deployment`

        Parameters
        ----------
        project_key : str
            Project identifier.
        query : str
            FTS5 query string.
        limit : int
            Max results to return.

        Returns
        -------
        list[dict]
            Each entry: { id, subject, importance, ack_required, created_ts, thread_id, from }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"10","method":"tools/call","params":{"name":"search_messages","arguments":{
          "project_key":"/abs/path/backend","query":"\"build plan\" AND users", "limit": 50
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        if project.id is None:
            raise ValueError("Project must have an id before searching messages.")
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                           m.thread_id, a.name AS sender_name
                    FROM fts_messages fm
                    JOIN messages m ON fm.message_id = m.id
                    JOIN agents a ON m.sender_id = a.id
                    WHERE m.project_id = :project_id AND fm MATCH :query
                    ORDER BY bm25(fm) ASC
                    LIMIT :limit
                    """
                ),
                {"project_id": project.id, "query": query, "limit": limit},
            )
            rows = result.mappings().all()
        await ctx.info(f"Search '{query}' returned {len(rows)} messages for project '{project.human_key}'.")
        return [
            {
                "id": row["id"],
                "subject": row["subject"],
                "importance": row["importance"],
                "ack_required": row["ack_required"],
                "created_ts": _iso(row["created_ts"]),
                "thread_id": row["thread_id"],
                "from": row["sender_name"],
            }
            for row in rows
        ]

    @mcp.tool(name="summarize_thread")
    async def summarize_thread(
        ctx: Context,
        project_key: str,
        thread_id: str,
        include_examples: bool = False,
    ) -> dict[str, Any]:
        """
        Extract participants, key points, and action items for a thread.

        Notes
        -----
        - If `thread_id` is not an id present on any message, it is treated as a string key
        - If `thread_id` is a message id, messages where `id == thread_id` are also included
        - `include_examples` returns up to 3 sample messages for quick preview

        Suggested use
        -------------
        - Call after a long discussion to inform a summarizing or planning agent.
        - Use `key_points` to seed a TODO list and `action_items` to assign work.

        Returns
        -------
        dict
            { thread_id, summary: {participants[], key_points[], action_items[], total_messages}, examples[] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"11","method":"tools/call","params":{"name":"summarize_thread","arguments":{
          "project_key":"/abs/path/backend","thread_id":"TKT-123","include_examples":true
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        if project.id is None:
            raise ValueError("Project must have an id before summarizing threads.")
        await ensure_schema()
        sender_alias = aliased(Agent)
        try:
            message_id = int(thread_id)
        except ValueError:
            message_id = None
        criteria = [Message.thread_id == thread_id]
        if message_id is not None:
            criteria.append(Message.id == message_id)
        async with get_session() as session:
            stmt = (
                select(Message, sender_alias.name)
                .join(sender_alias, Message.sender_id == sender_alias.id)
                .where(Message.project_id == project.id, or_(*criteria))
                .order_by(asc(Message.created_ts))
            )
            result = await session.execute(stmt)
            rows = result.all()
        summary = _summarize_messages(rows)
        examples = []
        if include_examples:
            for message, sender_name in rows[:3]:
                examples.append(
                    {
                        "id": message.id,
                        "subject": message.subject,
                        "from": sender_name,
                        "created_ts": _iso(message.created_ts),
                    }
                )
        await ctx.info(
            f"Summarized thread '{thread_id}' for project '{project.human_key}' with {len(rows)} messages"
        )
        return {"thread_id": thread_id, "summary": summary, "examples": examples}

    @mcp.tool(name="install_precommit_guard")
    async def install_precommit_guard(
        ctx: Context,
        project_key: str,
        code_repo_path: str,
    ) -> dict[str, Any]:
        project = await _get_project_by_identifier(project_key)
        archive = await ensure_archive(settings, project.slug)
        repo_path = Path(code_repo_path).expanduser().resolve()
        hooks_dir = repo_path / ".git" / "hooks"
        if not hooks_dir.is_dir():
            raise ValueError(f"No git hooks directory at {hooks_dir}")
        hook_path = hooks_dir / "pre-commit"
        script = await _build_precommit_hook_content(archive)
        await asyncio.to_thread(hook_path.write_text, script, "utf-8")
        await asyncio.to_thread(os.chmod, hook_path, 0o755)
        await ctx.info(f"Installed pre-commit guard for project '{project.human_key}' at {hook_path}.")
        return {"hook": str(hook_path)}

    @mcp.tool(name="uninstall_precommit_guard")
    async def uninstall_precommit_guard(
        ctx: Context,
        code_repo_path: str,
    ) -> dict[str, Any]:
        repo_path = Path(code_repo_path).expanduser().resolve()
        hook_path = repo_path / ".git" / "hooks" / "pre-commit"
        if hook_path.exists():
            await asyncio.to_thread(hook_path.unlink)
            await ctx.info(f"Removed pre-commit guard at {hook_path}.")
            return {"removed": True}
        await ctx.info(f"No pre-commit guard to remove at {hook_path}.")
        return {"removed": False}

    @mcp.tool(name="claim_paths")
    async def claim_paths(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """
        Request advisory claims (leases) on project-relative paths/globs.

        Semantics
        ---------
        - Conflicts are reported if an overlapping active exclusive claim exists held by another agent
        - Glob matching is symmetric (`fnmatchcase(a,b)` or `fnmatchcase(b,a)`), including exact matches
        - When granted, a JSON artifact is written under `claims/<sha1(path)>.json` and the DB is updated
        - TTL must be >= 60 seconds (enforced by the server settings/policy)

        Parameters
        ----------
        project_key : str
        agent_name : str
        paths : list[str]
            File paths or glob patterns relative to the project workspace (e.g., "app/api/*.py").
        ttl_seconds : int
            Time to live for the claim; expired claims are auto-released.
        exclusive : bool
            If true, exclusive intent; otherwise shared/observe-only.
        reason : str
            Optional explanation (helps humans reviewing Git artifacts).

        Returns
        -------
        dict
            { granted: [{id, path_pattern, exclusive, reason, expires_ts}], conflicts: [{path, holders: [...]}] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"12","method":"tools/call","params":{"name":"claim_paths","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle","paths":["app/api/*.py"],
          "ttl_seconds":7200,"exclusive":true,"reason":"migrations"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        if project.id is None:
            raise ValueError("Project must have an id before claiming paths.")
        await _expire_stale_claims(project.id)
        project_id = project.id
        async with get_session() as session:
            existing_rows = await session.execute(
                select(Claim, Agent.name)
                .join(Agent, Claim.agent_id == Agent.id)
                .where(
                    Claim.project_id == project_id,
                    cast(Any, Claim.released_ts).is_(None),
                    Claim.expires_ts > datetime.now(timezone.utc),
                )
            )
            existing_claims = existing_rows.all()

        granted: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        archive = await ensure_archive(settings, project.slug)
        async with AsyncFileLock(archive.lock_path):
            for path in paths:
                conflicting_holders: list[dict[str, Any]] = []
                for claim_record, holder_name in existing_claims:
                    if _claims_conflict(claim_record, path, exclusive, agent):
                        conflicting_holders.append(
                            {
                                "agent": holder_name,
                                "path_pattern": claim_record.path_pattern,
                                "exclusive": claim_record.exclusive,
                                "expires_ts": _iso(claim_record.expires_ts),
                            }
                        )
                if conflicting_holders:
                    conflicts.append({"path": path, "holders": conflicting_holders})
                    continue

                claim = await _create_claim(project, agent, path, exclusive, reason, ttl_seconds)
                claim_payload = {
                    "id": claim.id,
                    "project": project.human_key,
                    "agent": agent.name,
                    "path_pattern": claim.path_pattern,
                    "exclusive": claim.exclusive,
                    "reason": claim.reason,
                    "created_ts": _iso(claim.created_ts),
                    "expires_ts": _iso(claim.expires_ts),
                }
                await write_claim_record(archive, claim_payload)
                granted.append(
                    {
                        "id": claim.id,
                        "path_pattern": claim.path_pattern,
                        "exclusive": claim.exclusive,
                        "reason": claim.reason,
                        "expires_ts": _iso(claim.expires_ts),
                    }
                )
                existing_claims.append((claim, agent.name))
        await ctx.info(f"Issued {len(granted)} claims for '{agent.name}'. Conflicts: {len(conflicts)}")
        return {"granted": granted, "conflicts": conflicts}

    @mcp.tool(name="release_claims")
    async def release_claims_tool(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: Optional[list[str]] = None,
        claim_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """
        Release active claims held by an agent.

        Behavior
        --------
        - If both `paths` and `claim_ids` are omitted, all active claims for the agent are released
        - Otherwise, restricts release to matching ids and/or path patterns
        - JSON artifacts stay in Git for audit; DB records get `released_ts`

        Returns
        -------
        dict
            { released: int, released_at: iso8601 }

        Examples
        --------
        Release all active claims for agent:
        ```json
        {"jsonrpc":"2.0","id":"13","method":"tools/call","params":{"name":"release_claims","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle"
        }}}
        ```

        Release by ids:
        ```json
        {"jsonrpc":"2.0","id":"14","method":"tools/call","params":{"name":"release_claims","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle","claim_ids":[101,102]
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before releasing claims.")
        await ensure_schema()
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            stmt = (
                update(Claim)
                .where(
                    Claim.project_id == project.id,
                    Claim.agent_id == agent.id,
                    cast(Any, Claim.released_ts).is_(None),
                )
                .values(released_ts=now)
            )
            if claim_ids:
                stmt = stmt.where(cast(Any, Claim.id).in_(claim_ids))
            if paths:
                stmt = stmt.where(cast(Any, Claim.path_pattern).in_(paths))
            result = await session.execute(stmt)
            await session.commit()
        affected = int(result.rowcount or 0)
        await ctx.info(f"Released {affected} claims for '{agent.name}'.")
        return {"released": affected, "released_at": _iso(now)}

    @mcp.resource("resource://config/environment", mime_type="application/json")
    def environment_resource() -> dict[str, Any]:
        """
        Inspect the server's current environment and HTTP settings.

        When to use
        -----------
        - Debugging client connection issues (wrong host/port/path).
        - Verifying which environment (dev/stage/prod) the server is running in.

        Notes
        -----
        - This surfaces configuration only; it does not perform live health checks.

        Returns
        -------
        dict
            {
              "environment": str,
              "database_url": str,
              "http": { "host": str, "port": int, "path": str }
            }

        Example (JSON-RPC)
        ------------------
        ```json
        {"jsonrpc":"2.0","id":"r1","method":"resources/read","params":{"uri":"resource://config/environment"}}
        ```
        """
        return {
            "environment": settings.environment,
            "database_url": settings.database.url,
            "http": {
                "host": settings.http.host,
                "port": settings.http.port,
                "path": settings.http.path,
            },
        }

    @mcp.resource("resource://projects", mime_type="application/json")
    async def projects_resource() -> list[dict[str, Any]]:
        """
        List all projects known to the server in creation order.

        When to use
        -----------
        - Discover available projects when a user provides only an agent name.
        - Build UIs that let operators switch context between projects.

        Returns
        -------
        list[dict]
            Each: { id, slug, human_key, created_at }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r2","method":"resources/read","params":{"uri":"resource://projects"}}
        ```
        """
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(select(Project).order_by(asc(Project.created_at)))
            projects = result.scalars().all()
            return [_project_to_dict(project) for project in projects]

    @mcp.resource("resource://project/{slug}", mime_type="application/json")
    async def project_detail(slug: str) -> dict[str, Any]:
        """
        Fetch a project and its agents by project slug or human key.

        When to use
        -----------
        - Populate an "LDAP-like" directory for agents in tooling UIs.
        - Determine available agent identities and their metadata before addressing mail.

        Parameters
        ----------
        slug : str
            Project slug (or human key; both resolve to the same target).

        Returns
        -------
        dict
            Project descriptor including { agents: [...] } with agent profiles.

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r3","method":"resources/read","params":{"uri":"resource://project/backend-abc123"}}
        ```
        """
        project = await _get_project_by_identifier(slug)
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(select(Agent).where(Agent.project_id == project.id))
            agents = result.scalars().all()
        return {
            **_project_to_dict(project),
            "agents": [_agent_to_dict(agent) for agent in agents],
        }

    @mcp.resource("resource://claims/{slug}{?active_only}", mime_type="application/json")
    async def claims_resource(slug: str, active_only: bool = True) -> list[dict[str, Any]]:
        """
        List claims for a project, optionally filtering to active-only.

        Why this exists
        ---------------
        - Claims communicate edit intent and reduce collisions across agents.
        - Surfacing them helps humans review ongoing work and resolve contention.

        Parameters
        ----------
        slug : str
            Project slug or human key.
        active_only : bool
            If true (default), only returns claims with no `released_ts`.

        Returns
        -------
        list[dict]
            Each claim with { id, agent, path_pattern, exclusive, reason, created_ts, expires_ts, released_ts }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r4","method":"resources/read","params":{"uri":"resource://claims/backend-abc123?active_only=true"}}
        ```

        Also see all historical (including released) claims:
        ```json
        {"jsonrpc":"2.0","id":"r4b","method":"resources/read","params":{"uri":"resource://claims/backend-abc123?active_only=false"}}
        ```
        """
        project = await _get_project_by_identifier(slug)
        await ensure_schema()
        if project.id is None:
            raise ValueError("Project must have an id before listing claims.")
        await _expire_stale_claims(project.id)
        async with get_session() as session:
            stmt = select(Claim, Agent.name).join(Agent, Claim.agent_id == Agent.id).where(Claim.project_id == project.id)
            if active_only:
                stmt = stmt.where(cast(Any, Claim.released_ts).is_(None))
            result = await session.execute(stmt)
            rows = result.all()
        return [
            {
                "id": claim.id,
                "agent": holder,
                "path_pattern": claim.path_pattern,
                "exclusive": claim.exclusive,
                "reason": claim.reason,
                "created_ts": _iso(claim.created_ts),
                "expires_ts": _iso(claim.expires_ts),
                "released_ts": _iso(claim.released_ts) if claim.released_ts else None,
            }
            for claim, holder in rows
        ]

    @mcp.resource("resource://message/{message_id}{?project}", mime_type="application/json")
    async def message_resource(message_id: str, project: Optional[str] = None) -> dict[str, Any]:
        """
        Read a single message by id within a project.

        When to use
        -----------
        - Fetch the canonical body/metadata for rendering in a client after list/search.
        - Retrieve attachments and full details for a given message id.

        Parameters
        ----------
        message_id : str
            Numeric id as a string.
        project : str
            Project slug or human key (required for disambiguation).

        Common mistakes
        ---------------
        - Omitting `project` when a message id might exist in multiple projects.

        Returns
        -------
        dict
            Full message payload including body and sender name.

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r5","method":"resources/read","params":{"uri":"resource://message/1234?project=/abs/path/backend"}}
        ```
        """
        if project is None:
            raise ValueError("project parameter is required for message resource")
        project_obj = await _get_project_by_identifier(project)
        message = await _get_message(project_obj, int(message_id))
        sender = await _get_agent_by_id(project_obj, message.sender_id)
        payload = _message_to_dict(message, include_body=True)
        payload["from"] = sender.name
        return payload

    @mcp.resource("resource://thread/{thread_id}{?project,include_bodies}", mime_type="application/json")
    async def thread_resource(
        thread_id: str,
        project: Optional[str] = None,
        include_bodies: bool = False,
    ) -> dict[str, Any]:
        """
        List messages for a thread within a project.

        When to use
        -----------
        - Present a conversation view for a given ticket/thread key.
        - Export a thread for summarization or reporting.

        Parameters
        ----------
        thread_id : str
            Either a string thread key or a numeric message id to seed the thread.
        project : str
            Project slug or human key (required).
        include_bodies : bool
            Include message bodies if true (default false).

        Returns
        -------
        dict
            { project, thread_id, messages: [{...}] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r6","method":"resources/read","params":{"uri":"resource://thread/TKT-123?project=/abs/path/backend&include_bodies=true"}}
        ```

        Numeric seed example (message id as thread seed):
        ```json
        {"jsonrpc":"2.0","id":"r6b","method":"resources/read","params":{"uri":"resource://thread/1234?project=/abs/path/backend"}}
        ```
        """
        if project is None:
            raise ValueError("project parameter is required for thread resource")
        project_obj = await _get_project_by_identifier(project)
        if project_obj.id is None:
            raise ValueError("Project must have an id before listing threads.")
        await ensure_schema()
        try:
            message_id = int(thread_id)
        except ValueError:
            message_id = None
        sender_alias = aliased(Agent)
        criteria = [Message.thread_id == thread_id]
        if message_id is not None:
            criteria.append(Message.id == message_id)
        async with get_session() as session:
            stmt = (
                select(Message, sender_alias.name)
                .join(sender_alias, Message.sender_id == sender_alias.id)
                .where(Message.project_id == project_obj.id, or_(*criteria))
                .order_by(asc(Message.created_ts))
            )
            result = await session.execute(stmt)
            rows = result.all()
        messages = []
        for message, sender_name in rows:
            payload = _message_to_dict(message, include_body=include_bodies)
            payload["from"] = sender_name
            messages.append(payload)
        return {"project": project_obj.human_key, "thread_id": thread_id, "messages": messages}

    @mcp.resource(
        "resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit}",
        mime_type="application/json",
    )
    async def inbox_resource(
        agent: str,
        project: Optional[str] = None,
        since_ts: Optional[str] = None,
        urgent_only: bool = False,
        include_bodies: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Read an agent's inbox for a project.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        since_ts : Optional[str]
            ISO-8601 timestamp string; only messages newer than this are returned.
        urgent_only : bool
            If true, limits to importance in {high, urgent}.
        include_bodies : bool
            Include message bodies in results (default false).
        limit : int
            Maximum number of messages to return (default 20).

        Returns
        -------
        dict
            { project, agent, count, messages: [...] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r7","method":"resources/read","params":{"uri":"resource://inbox/BlueLake?project=/abs/path/backend&limit=10&urgent_only=true"}}
        ```
        Incremental fetch example (using since_ts):
        ```json
        {"jsonrpc":"2.0","id":"r7b","method":"resources/read","params":{"uri":"resource://inbox/BlueLake?project=/abs/path/backend&since_ts=2025-10-23T15:00:00Z"}}
        ```
        """
        if project is None:
            raise ValueError("project parameter is required for inbox resource")
        project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        messages = await _list_inbox(project_obj, agent_obj, limit, urgent_only, include_bodies, since_ts)
        return {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "count": len(messages),
            "messages": messages,
        }

    return mcp

async def _build_precommit_hook_content(archive: ProjectArchive) -> str:
    claims_dir = archive.root / "claims"
    storage_root = archive.root
    template = Template(
        textwrap.dedent(
            """#!/usr/bin/env python3
            import json
            import os
            import sys
            import subprocess
            from pathlib import Path
            from fnmatch import fnmatch
            from datetime import datetime, timezone

            CLAIMS_DIR = Path("$claims_dir")
            STORAGE_ROOT = Path("$storage_root")
            AGENT_NAME = os.environ.get("AGENT_NAME")
            if not AGENT_NAME:
                sys.stderr.write("[pre-commit] AGENT_NAME environment variable is required.\n")
                sys.exit(1)

            if not CLAIMS_DIR.exists():
                sys.exit(0)

            now = datetime.now(timezone.utc)

            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
            )
            if staged.returncode != 0:
                sys.stderr.write("[pre-commit] Failed to enumerate staged files.\n")
                sys.exit(1)

            paths = [line.strip() for line in staged.stdout.splitlines() if line.strip()]

            if not paths:
                sys.exit(0)

            def load_claims():
                for candidate in CLAIMS_DIR.glob("*.json"):
                    try:
                        data = json.loads(candidate.read_text())
                    except Exception:
                        continue
                    yield data

            conflicts = []
            for claim in load_claims():
                if claim.get("agent") == AGENT_NAME:
                    continue
                expires = claim.get("expires_ts")
                if expires:
                    try:
                        expires_dt = datetime.fromisoformat(expires)
                        if expires_dt < now:
                            continue
                    except Exception:
                        pass
                pattern = claim.get("path_pattern")
                if not pattern:
                    continue
                for path_value in paths:
                    if fnmatch(path_value, pattern) or fnmatch(pattern, path_value):
                        conflicts.append((path_value, claim.get("agent"), pattern))

            if conflicts:
                sys.stderr.write("[pre-commit] Exclusive claim conflicts detected:\n")
                for path_value, agent_name, pattern in conflicts:
                    sys.stderr.write("  - {} matches claim '{}' held by {}\n".format(path_value, pattern, agent_name))
                sys.stderr.write("Resolve conflicts or release claims before committing.\n")
                sys.exit(1)

            sys.exit(0)
            """
        )
    )
    return template.substitute(claims_dir=str(claims_dir), storage_root=str(storage_root))
