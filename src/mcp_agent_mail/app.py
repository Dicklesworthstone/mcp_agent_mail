"""Application factory for the MCP Agent Mail server."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastmcp import Context, FastMCP
from sqlalchemy import asc, desc, func, select
from sqlalchemy.exc import NoResultFound

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
) -> Message:
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


async def _list_inbox(project: Project, agent: Agent, limit: int) -> list[dict[str, Any]]:
    await ensure_schema()
    async with get_session() as session:
        stmt = (
            select(Message)
            .join(MessageRecipient, MessageRecipient.message_id == Message.id)
            .where(
                Message.project_id == project.id,
                MessageRecipient.agent_id == agent.id,
            )
            .order_by(desc(Message.created_ts))
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = result.scalars().all()
        return [_message_to_dict(message, include_body=False) for message in messages]


def build_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server instance."""
    settings: Settings = get_settings()
    lifespan = _lifespan_factory(settings)

    instructions = (
        "You are the MCP Agent Mail coordination server. "
        "Provide message routing, coordination tooling, and project context to cooperating agents."
    )

    mcp = FastMCP(name="mcp-agent-mail", instructions=instructions, lifespan=lifespan)

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
        archive = await ensure_archive(settings, project.slug)
        async with AsyncFileLock(archive.lock_path):
            pass
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
        to_agents = [await _get_agent(project, name) for name in to]
        cc_names = cc or []
        bcc_names = bcc or []
        cc_agents = [await _get_agent(project, name) for name in cc_names]
        bcc_agents = [await _get_agent(project, name) for name in bcc_names]
        recipient_records: list[tuple[Agent, str]] = [(agent, "to") for agent in to_agents]
        recipient_records.extend((agent, "cc") for agent in cc_agents)
        recipient_records.extend((agent, "bcc") for agent in bcc_agents)

        archive = await ensure_archive(settings, project.slug)
        convert_markdown = convert_images if convert_images is not None else True
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
            await write_message_bundle(
                archive,
                frontmatter,
                processed_body,
                sender.name,
                [agent.name for agent in to_agents + cc_agents + bcc_agents],
                attachment_files,
            )
        await ctx.info(f"Message '{message.id}' created by '{sender.name}'.")
        payload = _message_to_dict(message)
        payload["from"] = sender.name
        payload["to"] = [agent.name for agent in to_agents]
        payload["cc"] = [agent.name for agent in cc_agents]
        payload["bcc"] = [agent.name for agent in bcc_agents]
        payload["attachments"] = attachments_meta
        return payload

    @mcp.tool(name="fetch_inbox")
    async def fetch_inbox(
        ctx: Context,
        project_key: str,
        agent_name: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        items = await _list_inbox(project, agent, limit)
        await ctx.info(f"Fetched {len(items)} messages for '{agent.name}'.")
        return items

    @mcp.tool(name="claim_paths")
    async def claim_paths(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        claims = []
        archive = await ensure_archive(settings, project.slug)
        async with AsyncFileLock(archive.lock_path):
            for path in paths:
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
                claims.append(
                    {
                        "id": claim.id,
                        "path_pattern": claim.path_pattern,
                        "exclusive": claim.exclusive,
                        "reason": claim.reason,
                        "expires_ts": _iso(claim.expires_ts),
                    }
                )
        await ctx.info(f"Issued {len(claims)} claims for '{agent.name}'.")
        return claims

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

    return mcp
