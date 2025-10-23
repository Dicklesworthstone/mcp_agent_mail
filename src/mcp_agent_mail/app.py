"""Application factory for the MCP Agent Mail server."""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from fastmcp import Context, FastMCP
from sqlalchemy import asc, desc, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import aliased

from .config import Settings, get_settings
from .db import ensure_schema, get_session, init_engine
from .models import Agent, Claim, Message, MessageRecipient, Project
from .storage import (
    AsyncFileLock,
    ensure_archive,
    process_attachments,
    write_agent_profile,
    write_claim_record,
    write_message_bundle,
)
from .utils import generate_agent_name, slugify


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
    desired_name = (name or generate_agent_name()).strip()
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
        project = await _get_project_by_identifier(project_key)
        agent = await _get_or_create_agent(project, name, program, model, task_description, settings)
        await ctx.info(f"Registered agent '{agent.name}' for project '{project.human_key}'.")
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
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(select(Project).order_by(asc(Project.created_at)))
            projects = result.scalars().all()
            return [_project_to_dict(project) for project in projects]

    @mcp.resource("resource://project/{slug}", mime_type="application/json")
    async def project_detail(slug: str) -> dict[str, Any]:
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
