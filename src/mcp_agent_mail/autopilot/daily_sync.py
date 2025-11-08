"""Daily 04:00 executive sync orchestration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import NoResultFound

from ..config import get_settings
from ..db import ensure_schema, get_session
from ..models import Agent, Project
from .ledger import TaskRecord, bead_balances, fetch_open_tasks, get_agent_map, resolve_project


DEFAULT_EXECUTIVES = ("GreenPresident", "WhiteFox", "BlackDog")
DEFAULT_PROJECT_KEY = "/Users/nickflorez/Projects/win"
AUTOPILOT_AGENT_NAME = "Autopilot"


def _load_local_env() -> None:
    """Load .env file if present for local execution."""

    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class AgentBrief:
    agent: Agent
    bead_balance: int
    assigned: list[dict[str, Any]]
    backlog: list[dict[str, Any]]
    message_body: str
    thread_id: str


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_thread_id(agent_name: str, run_id: str) -> str:
    return f"daily-sync-{agent_name.lower()}-{run_id}"


def _format_task_summary(task: dict[str, Any]) -> str:
    due = task.get("due_ts")
    due_str = "—" if not due else datetime.fromisoformat(due).strftime("%Y-%m-%d")
    bead = task.get("bead_value", 0)
    return f"- **#{task['id']}** · {task['title']} · Due: {due_str} · Beads: {bead}\n  {task['description'].strip() or '_No description_' }"


def _render_brief(agent_name: str, bead_balance: int, assigned: list[dict[str, Any]], backlog: list[dict[str, Any]]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Daily Sync Brief — {agent_name}", "", f"Date: {today}", f"Current Beads: {bead_balance}", ""]

    lines.append("## Assigned Tasks")
    if assigned:
        for task in assigned:
            lines.append(_format_task_summary(task))
    else:
        lines.append("- _No owned tasks currently_")

    lines.append("")
    lines.append("## Unassigned / Backlog Opportunities")
    if backlog:
        for task in backlog:
            lines.append(_format_task_summary(task))
    else:
        lines.append("- _No open backlog items_")

    lines.extend(
        [
            "",
            "## Required Actions",
            "1. Confirm ownership or reassign as needed",
            "2. Provide bead adjustments in response using `BEAD_TXN` markers",
            "3. Flag risks or dependencies blocking progress",
            "",
            "— Autopilot Orchestrator",
        ]
    )
    return "\n".join(lines)


async def _prepare_briefs(
    *,
    project: Project,
    agent_names: Iterable[str],
    run_id: str,
    tasks: list[TaskRecord],
) -> list[AgentBrief]:
    agent_map = await get_agent_map(project.id, agent_names)
    missing = [name for name in agent_names if name not in agent_map]
    if missing:
        project_identifier = project.human_key or project.slug or str(project.id)
        raise RuntimeError(
            f"Agents not registered for project {project_identifier}: {', '.join(missing)}"
        )

    serialised = []
    for record in tasks:
        serialised.append(
            {
                "id": record.id,
                "title": record.title,
                "status": record.status,
                "priority": record.priority,
                "bead_value": record.bead_value,
                "due_ts": record.due_ts.isoformat() if record.due_ts else None,
                "owner": record.owner_agent,
                "description": record.description,
            }
        )

    balances = await bead_balances(agent.id for agent in agent_map.values())
    briefs: list[AgentBrief] = []
    for name, agent in agent_map.items():
        assigned = [task for task in serialised if task.get("owner") == name]
        backlog = [task for task in serialised if task.get("owner") is None]
        message_body = _render_brief(
            name,
            balances.get(agent.id, 0),
            assigned,
            backlog,
        )
        briefs.append(
            AgentBrief(
                agent=agent,
                bead_balance=balances.get(agent.id, 0),
                assigned=assigned,
                backlog=backlog,
                message_body=message_body,
                thread_id=_build_thread_id(name, run_id),
            )
        )
    return briefs


async def _ensure_autopilot_agent(project: Project) -> Agent:
    """Ensure the scheduler agent exists in the roster."""

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, Agent.name == AUTOPILOT_AGENT_NAME)
        )
        agent = result.scalars().first()
        if agent:
            return agent

        now = datetime.now(timezone.utc)
        agent = Agent(
            project_id=project.id,
            name=AUTOPILOT_AGENT_NAME,
            program="scheduler",
            model="system",
            task_description="Autonomous orchestrator for daily executive sync",
            inception_ts=now,
            last_active_ts=now,
            attachments_policy="auto",
            contact_policy="auto",
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


_load_local_env()


def _normalized_async_database_url(url: str) -> str:
    """Return URL updated to use an async driver when possible."""

    try:
        parsed = make_url(url)
    except Exception:
        return url

    driver = parsed.drivername
    if driver.startswith("sqlite"):
        if "+aiosqlite" in driver:
            return url
        return str(parsed.set(drivername="sqlite+aiosqlite"))
    if driver.startswith("postgresql"):
        if "+asyncpg" in driver:
            return url
        return str(parsed.set(drivername="postgresql+asyncpg"))
    if driver.startswith("mysql"):
        if "+aiomysql" in driver:
            return url
        return str(parsed.set(drivername="mysql+aiomysql"))
    return url


async def _send_messages(
    *,
    briefs: Iterable[AgentBrief],
    project_key: str,
    bearer_token: str,
    sender_name: str,
    dry_run: bool,
    mcp_endpoint: str,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for brief in briefs:
            payload = {
                "jsonrpc": "2.0",
                "id": int(datetime.now().timestamp() * 1000),
                "method": "tools/call",
                "params": {
                    "name": "send_message",
                    "arguments": {
                        "project_key": project_key,
                        "sender_name": sender_name,
                        "to": [brief.agent.name],
                        "subject": f"Daily Sync: {brief.agent.name}",
                        "body_md": brief.message_body,
                        "thread_id": brief.thread_id,
                        "importance": "high",
                    },
                },
            }
            if dry_run:
                results.append({"agent": brief.agent.name, "thread_id": brief.thread_id, "status": "dry-run"})
                continue

            response = await client.post(mcp_endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            results.append({"agent": brief.agent.name, "thread_id": brief.thread_id, "response": data})
    return results


async def run_daily_sync(
    *,
    project_key: str = DEFAULT_PROJECT_KEY,
    agent_names: Iterable[str] = DEFAULT_EXECUTIVES,
    dry_run: bool = False,
    wait_seconds: int = 0,
    run_id: str | None = None,
    bearer_token: str | None = None,
    mcp_endpoint: str | None = None,
    task_limit: int = 25,
) -> dict[str, Any]:
    """Execute daily sync orchestration."""

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///messages.db"
    else:
        normalized_url = _normalized_async_database_url(db_url)
        if normalized_url != db_url:
            os.environ["DATABASE_URL"] = normalized_url

    await ensure_schema(get_settings())
    resolved_run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    project = await resolve_project(project_key)
    autopilot_agent = await _ensure_autopilot_agent(project)
    task_records = await fetch_open_tasks(project.id, limit=task_limit)
    briefs = await _prepare_briefs(project=project, agent_names=agent_names, run_id=resolved_run_id, tasks=task_records)

    token = bearer_token or os.environ.get("HTTP_BEARER_TOKEN") or os.environ.get("MCP_BEARER_TOKEN")
    if not token and not dry_run:
        raise RuntimeError("HTTP bearer token must be provided via --bearer-token or environment")

    endpoint = mcp_endpoint or os.environ.get("MCP_ENDPOINT", "http://127.0.0.1:8765/mcp/")
    send_results = await _send_messages(
        briefs=briefs,
        project_key=project_key,
        bearer_token=token or "",
        sender_name=autopilot_agent.name,
        dry_run=dry_run,
        mcp_endpoint=endpoint,
    )

    run_summary = {
        "run_id": resolved_run_id,
        "timestamp": _iso_now(),
        "project_key": project_key,
        "dry_run": dry_run,
        "wait_seconds": wait_seconds,
        "task_limit": task_limit,
        "agents": [
            {
                "agent": brief.agent.name,
                "beads": brief.bead_balance,
                "assigned": brief.assigned,
                "backlog": brief.backlog,
                "thread_id": brief.thread_id,
            }
            for brief in briefs
        ],
        "send_results": send_results,
    }

    _write_run_log(run_summary)

    if wait_seconds > 0 and not dry_run:
        await asyncio.sleep(wait_seconds)
        # Response harvesting would be implemented here in future iterations.

    return run_summary


def _write_run_log(summary: dict[str, Any]) -> None:
    logs_root = Path("logs/autopilot")
    logs_root.mkdir(parents=True, exist_ok=True)
    filename = logs_root / f"daily_sync_{summary['run_id']}.json"
    with filename.open("w", encoding="utf-8") as fp:
        json.dump(summary, fp, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the 04:00 executive sync pilot.")
    parser.add_argument("--project-key", default=DEFAULT_PROJECT_KEY, help="Project human key/slug to target")
    parser.add_argument(
        "--agents",
        nargs="*",
        default=list(DEFAULT_EXECUTIVES),
        help="Agent names to receive the briefing",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip sending messages and just log output")
    parser.add_argument("--wait-seconds", type=int, default=0, help="Optional wait period to harvest responses later")
    parser.add_argument("--run-id", help="Explicit run identifier (defaults to timestamp)")
    parser.add_argument("--bearer-token", help="HTTP bearer token override")
    parser.add_argument("--endpoint", help="MCP endpoint override (default http://127.0.0.1:8765/mcp/)")
    parser.add_argument("--task-limit", type=int, default=25, help="Maximum tasks to load from ledger (default 25)")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        summary = asyncio.run(
            run_daily_sync(
                project_key=args.project_key,
                agent_names=args.agents,
                dry_run=args.dry_run,
                wait_seconds=args.wait_seconds,
                run_id=args.run_id,
                bearer_token=args.bearer_token,
                mcp_endpoint=args.endpoint,
                task_limit=args.task_limit,
            )
        )
    except NoResultFound as exc:
        parser.error(str(exc))
        return

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":  # pragma: no cover - manual entry
    main()
