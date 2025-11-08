"""Database helpers for task and bead ledger management."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound

from ..db import get_session
from ..models import Agent, BeadTransaction, Project, Task
from ..utils import slugify


BEADS_DB_DEFAULT = Path(__file__).resolve().parents[4] / ".beads" / "beads.db"


@dataclass(slots=True)
class TaskRecord:
    id: str
    title: str
    status: str
    priority: str
    bead_value: int
    due_ts: Optional[datetime]
    owner_agent: Optional[str]
    description: str


async def resolve_project(project_key: str) -> Project:
    """Return project row for given human key or slug."""

    slug = slugify(project_key)
    async with get_session() as session:
        result = await session.execute(
            select(Project).where((Project.slug == slug) | (Project.human_key == project_key))
        )
        project = result.scalars().first()
        if not project:
            raise NoResultFound(f"Project not found for key: {project_key}")
        return project


async def fetch_open_tasks(project_id: int, *, limit: int = 100) -> list[TaskRecord]:
    """Return open or in-progress tasks sourced from internal table or Beads DB."""

    sqlmodel_tasks = await _fetch_sqlmodel_tasks(project_id, limit)
    if sqlmodel_tasks:
        return sqlmodel_tasks
    return _fetch_beads_tasks(limit)


async def _fetch_sqlmodel_tasks(project_id: int, limit: int) -> list[TaskRecord]:
    async with get_session() as session:
        result = await session.execute(
            select(Task, Agent.name)
            .outerjoin(Agent, Agent.id == Task.owner_agent_id)
            .where(Task.project_id == project_id, Task.status.in_(("open", "in_progress")))
            .order_by(Task.priority.desc(), Task.due_ts.is_(None), Task.due_ts.asc(), Task.created_ts.asc())
            .limit(limit)
        )

        records: list[TaskRecord] = []
        for task, owner_name in result.all():
            records.append(
                TaskRecord(
                    id=str(task.id),
                    title=task.title,
                    status=task.status,
                    priority=task.priority,
                    bead_value=task.bead_value,
                    due_ts=task.due_ts,
                    owner_agent=owner_name,
                    description=task.description or "",
                )
            )
        return records


def _resolve_beads_path() -> Optional[Path]:
    path = os.environ.get("BEADS_DB_PATH")
    if path:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return candidate
    if BEADS_DB_DEFAULT.exists():
        return BEADS_DB_DEFAULT
    return None


def _fetch_beads_tasks(limit: int) -> list[TaskRecord]:
    beads_path = _resolve_beads_path()
    if not beads_path:
        return []

    conn = sqlite3.connect(str(beads_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, title, status, priority, assignee, description, acceptance_criteria, notes, created_at
            FROM issues
            WHERE status = 'open'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    records: list[TaskRecord] = []
    for row in rows:
        notes_parts = [row["description"], row["acceptance_criteria"], row["notes"]]
        description = "\n\n".join(part.strip() for part in notes_parts if part and part.strip())
        bead_value = int(row["priority"] or 0)
        records.append(
            TaskRecord(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                priority=str(row["priority"] or ""),
                bead_value=bead_value,
                due_ts=None,
                owner_agent=row["assignee"] or None,
                description=description,
            )
        )
    return records


async def get_agent_map(project_id: int, agent_names: Iterable[str]) -> dict[str, Agent]:
    """Return mapping of agent name -> Agent row for project."""

    names = {name for name in agent_names}
    if not names:
        return {}

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project_id, Agent.name.in_(names))
        )
        agents = {agent.name: agent for agent in result.scalars()}
        return agents


async def bead_balances(agent_ids: Iterable[int]) -> dict[int, int]:
    """Compute current bead balance per agent id."""

    ids = list(agent_ids)
    if not ids:
        return {}

    async with get_session() as session:
        result = await session.execute(
            select(BeadTransaction.agent_id, func.coalesce(func.sum(BeadTransaction.delta), 0))
            .where(BeadTransaction.agent_id.in_(ids))
            .group_by(BeadTransaction.agent_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}


async def record_bead_transaction(
    *,
    agent_id: int,
    delta: int,
    reason: str,
    task_id: Optional[int],
    run_id: Optional[str],
) -> None:
    """Append a bead ledger entry."""

    async with get_session() as session:
        txn = BeadTransaction(
            agent_id=agent_id,
            task_id=task_id,
            delta=delta,
            reason=reason,
            run_id=run_id,
        )
        session.add(txn)
        await session.commit()
