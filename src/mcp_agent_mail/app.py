"""Application factory for the MCP Agent Mail server."""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import inspect
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import parse_qsl

from fastmcp import Context, FastMCP
from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from sqlalchemy import asc, desc, func, or_, select, text, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import aliased

from . import rich_logger
from .config import Settings, get_settings
from .db import ensure_schema, get_session, init_engine
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .llm import complete_system_user
from .models import Agent, AgentLink, FileReservation, Message, MessageRecipient, Project, ProjectSiblingSuggestion
from .storage import (
    ProjectArchive,
    archive_write_lock,
    collect_lock_status,
    ensure_archive,
    heal_archive_locks,
    process_attachments,
    write_agent_profile,
    write_file_reservation_record,
    write_message_bundle,
)
from .utils import generate_agent_name, sanitize_agent_name, slugify

logger = logging.getLogger(__name__)

TOOL_METRICS: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "errors": 0})
TOOL_CLUSTER_MAP: dict[str, str] = {}
TOOL_METADATA: dict[str, dict[str, Any]] = {}

RECENT_TOOL_USAGE: deque[tuple[datetime, str, Optional[str], Optional[str]]] = deque(maxlen=4096)

CLUSTER_SETUP = "infrastructure"
CLUSTER_IDENTITY = "identity"
CLUSTER_MESSAGING = "messaging"
CLUSTER_CONTACT = "contact"
CLUSTER_SEARCH = "search"
CLUSTER_FILE_RESERVATIONS = "file_reservations"
CLUSTER_MACROS = "workflow_macros"


class ToolExecutionError(Exception):
    def __init__(self, error_type: str, message: str, *, recoverable: bool = True, data: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.error_type = error_type
        self.recoverable = recoverable
        self.data = data or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "type": self.error_type,
                "message": str(self),
                "recoverable": self.recoverable,
                "data": self.data,
            }
        }


def _record_tool_error(tool_name: str, exc: Exception) -> None:
    logger.warning(
        "tool_error",
        extra={
            "tool": tool_name,
            "error": type(exc).__name__,
            "error_message": str(exc),
        },
    )


def _register_tool(name: str, metadata: dict[str, Any]) -> None:
    TOOL_CLUSTER_MAP[name] = metadata["cluster"]
    TOOL_METADATA[name] = metadata


def _bind_arguments(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> inspect.BoundArguments:
    try:
        return signature.bind_partial(*args, **kwargs)
    except TypeError:
        return signature.bind(*args, **kwargs)


def _extract_argument(bound: inspect.BoundArguments, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    value = bound.arguments.get(name)
    if value is None:
        return None
    return str(value)


def _enforce_capabilities(ctx: Context, required: set[str], tool_name: str) -> None:
    if not required:
        return
    metadata = getattr(ctx, "metadata", {}) or {}
    allowed = metadata.get("allowed_capabilities")
    if allowed is None:
        return
    allowed_set = {str(item) for item in allowed}
    if allowed_set and not required.issubset(allowed_set):
        missing = sorted(required - allowed_set)
        raise ToolExecutionError(
            "CAPABILITY_DENIED",
            f"Tool '{tool_name}' requires capabilities {missing} (allowed={sorted(allowed_set)}).",
            recoverable=False,
            data={"required": missing, "allowed": sorted(allowed_set)},
        )


def _record_recent(tool_name: str, project: Optional[str], agent: Optional[str]) -> None:
    RECENT_TOOL_USAGE.append((datetime.now(timezone.utc), tool_name, project, agent))


def _instrument_tool(
    tool_name: str,
    *,
    cluster: str,
    capabilities: Optional[set[str]] = None,
    complexity: str = "medium",
    agent_arg: Optional[str] = None,
    project_arg: Optional[str] = None,
):
    meta = {
        "cluster": cluster,
        "capabilities": sorted(capabilities or {cluster}),
        "complexity": complexity,
        "agent_arg": agent_arg,
        "project_arg": project_arg,
    }
    _register_tool(tool_name, meta)

    def decorator(func):
        signature = inspect.signature(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.perf_counter()

            metrics = TOOL_METRICS[tool_name]
            metrics["calls"] += 1
            bound = _bind_arguments(signature, args, kwargs)
            ctx = bound.arguments.get("ctx")
            if isinstance(ctx, Context) and meta["capabilities"]:
                required_caps = set(cast(list[str], meta["capabilities"]))
                _enforce_capabilities(ctx, required_caps, tool_name)
            project_value = _extract_argument(bound, project_arg)
            agent_value = _extract_argument(bound, agent_arg)

            # Rich logging: Log tool call start if enabled
            settings = get_settings()
            log_enabled = settings.tools_log_enabled
            log_ctx = None

            if log_enabled:
                try:
                    clean_kwargs = {k: v for k, v in bound.arguments.items() if k != "ctx"}
                    log_ctx = rich_logger.ToolCallContext(
                        tool_name=tool_name,
                        args=[],
                        kwargs=clean_kwargs,
                        project=project_value,
                        agent=agent_value,
                        start_time=start_time,
                    )
                    rich_logger.log_tool_call_start(log_ctx)
                except Exception:
                    # Logging errors should not break tool execution
                    log_ctx = None

            result = None
            error = None
            try:
                result = await func(*args, **kwargs)
            except ToolExecutionError as exc:
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error = exc
                raise
            except NoResultFound as exc:
                # Handle agent/project not found errors with helpful messages
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "NOT_FOUND",
                    str(exc),  # Use the original helpful error message
                    recoverable=True,
                    data={"tool": tool_name},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except Exception as exc:
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "UNHANDLED_EXCEPTION",
                    "Server encountered an unexpected error while executing tool.",
                    recoverable=False,
                    data={"tool": tool_name, "original_error": type(exc).__name__},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            finally:
                _record_recent(tool_name, project_value, agent_value)

                # Rich logging: Log tool call end if enabled
                if log_ctx is not None:
                    try:
                        log_ctx.end_time = time.perf_counter()
                        log_ctx.result = result
                        log_ctx.error = error
                        log_ctx.success = error is None
                        rich_logger.log_tool_call_end(log_ctx)
                    except Exception:
                        # Logging errors should not suppress original exceptions
                        pass

            return result

        # Preserve annotations so FastMCP can infer output schema
        with suppress(Exception):
            wrapper.__annotations__ = getattr(func, "__annotations__", {})
        return wrapper

    return decorator


def _tool_metrics_snapshot() -> list[dict[str, Any]]:
    snapshot = []
    for name, data in sorted(TOOL_METRICS.items()):
        metadata = TOOL_METADATA.get(name, {})
        snapshot.append(
            {
                "name": name,
                "calls": data["calls"],
                "errors": data["errors"],
                "cluster": TOOL_CLUSTER_MAP.get(name, "unclassified"),
                "capabilities": metadata.get("capabilities", []),
                "complexity": metadata.get("complexity", "unknown"),
            }
        )
    return snapshot


@functools.lru_cache(maxsize=1)
def _load_capabilities_mapping() -> list[dict[str, Any]]:
    mapping_path = Path(__file__).resolve().parent.parent.parent / "deploy" / "capabilities" / "agent_capabilities.json"
    if not mapping_path.exists():
        return []
    try:
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("capability_mapping.load_failed", extra={"error": str(exc)})
        return []
    agents = data.get("agents", [])
    if not isinstance(agents, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        normalized.append(entry)
    return normalized


def _capabilities_for(agent: Optional[str], project: Optional[str]) -> list[str]:
    mapping = _load_capabilities_mapping()
    caps: set[str] = set()
    for entry in mapping:
        entry_agent = entry.get("name")
        entry_project = entry.get("project")
        if agent and entry_agent != agent:
            continue
        if project and entry_project != project:
            continue
        for item in entry.get("capabilities", []):
            if isinstance(item, str):
                caps.add(item)
    return sorted(caps)


def _lifespan_factory(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastMCP):
        init_engine(settings)
        heal_summary = await heal_archive_locks(settings)
        if heal_summary.get("locks_removed") or heal_summary.get("metadata_removed"):
            logger.info(
                "archive.healed_on_startup",
                extra={
                    "locks_scanned": heal_summary.get("locks_scanned", 0),
                    "locks_removed": len(heal_summary.get("locks_removed", [])),
                    "metadata_removed": len(heal_summary.get("metadata_removed", [])),
                },
            )
        await ensure_schema(settings)
        yield

    return lifespan


def _iso(dt: Any) -> str:
    """Return ISO-8601 in UTC from datetime or best-effort from string.

    Accepts datetime or ISO-like string; falls back to str(dt) if unknown.
    """
    try:
        if isinstance(dt, str):
            try:
                parsed = datetime.fromisoformat(dt)
                return parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                return dt
        if hasattr(dt, "astimezone"):
            return dt.astimezone(timezone.utc).isoformat()  # type: ignore[no-any-return]
        return str(dt)
    except Exception:
        return str(dt)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _max_datetime(*timestamps: Optional[datetime]) -> Optional[datetime]:
    values = [ts for ts in timestamps if ts is not None]
    if not values:
        return None
    return max(values)


_TRUE_FLAG_VALUES: tuple[str, ...] = ("1", "true", "yes", "on", "y")
_FALSE_FLAG_VALUES: tuple[str, ...] = ("0", "false", "no", "off", "n")


def _split_slug_and_query(raw_value: str) -> tuple[str, dict[str, str]]:
    slug, _, query_string = raw_value.partition("?")
    if not query_string:
        return slug, {}
    params = cast(dict[str, str], dict(parse_qsl(query_string, keep_blank_values=True)))
    return slug, params


def _coerce_flag_to_bool(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_FLAG_VALUES:
        return True
    if normalized in _FALSE_FLAG_VALUES:
        return False
    return default


@dataclass(slots=True)
class FileReservationStatus:
    reservation: FileReservation
    agent: Agent
    stale: bool
    stale_reasons: list[str]
    last_agent_activity: Optional[datetime]
    last_mail_activity: Optional[datetime]
    last_fs_activity: Optional[datetime]
    last_git_activity: Optional[datetime]


_GLOB_MARKERS: tuple[str, ...] = ("*", "?", "[")


def _contains_glob(pattern: str) -> bool:
    return any(marker in pattern for marker in _GLOB_MARKERS)


def _normalize_pattern(pattern: str) -> str:
    return pattern.lstrip("/").strip()


def _collect_matching_paths(base: Path, pattern: str) -> list[Path]:
    if not base.exists():
        return []
    normalized = _normalize_pattern(pattern)
    if not normalized:
        return []
    if _contains_glob(normalized):
        return list(base.glob(normalized))
    candidate = base / normalized
    if not candidate.exists():
        return []
    return [candidate]


def _latest_filesystem_activity(paths: Sequence[Path]) -> Optional[datetime]:
    mtimes: list[datetime] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        mtimes.append(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
    if not mtimes:
        return None
    return max(mtimes)


def _latest_git_activity(repo: Optional[Repo], matches: Sequence[Path]) -> Optional[datetime]:
    if repo is None:
        return None
    repo_root = Path(repo.working_tree_dir or "").resolve()
    commit_times: list[datetime] = []
    for match in matches:
        try:
            rel_path = match.resolve().relative_to(repo_root)
        except Exception:
            continue
        try:
            commit = next(repo.iter_commits(paths=str(rel_path), max_count=1))
        except StopIteration:
            continue
        except Exception:
            continue
        commit_times.append(datetime.fromtimestamp(commit.committed_date, tz=timezone.utc))
    if not commit_times:
        return None
    return max(commit_times)


def _project_workspace_path(project: Project) -> Optional[Path]:
    try:
        candidate = Path(project.human_key).expanduser()
    except Exception:
        return None
    with suppress(OSError):
        if candidate.exists():
            return candidate
    return None


def _open_repo_if_available(workspace: Optional[Path]) -> Optional[Repo]:
    if workspace is None:
        return None
    try:
        repo = Repo(workspace, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None
    except Exception:
        return None
    try:
        root = Path(repo.working_tree_dir or "")
    except Exception:
        return None
    with suppress(Exception):
        workspace.resolve().relative_to(root.resolve())
        return repo
    return None


def _parse_json_safely(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction supporting code fences and stray text.

    Returns parsed dict on success, otherwise None.
    """
    import json as _json
    import re as _re

    try:
        parsed = _json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # Code fence block
    m = _re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        inner = m.group(1)
        try:
            parsed = _json.loads(inner)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    # Braces slice heuristic
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            parsed = _json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _parse_iso(raw_value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamps, accepting a trailing 'Z' as UTC.

    Returns None when parsing fails.
    """
    if raw_value is None:
        return None
    s = raw_value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _rich_error_panel(title: str, payload: dict[str, Any]) -> None:
    """Render a compact JSON error panel if Rich is available and tools logging is enabled."""
    try:
        if not get_settings().tools_log_enabled:
            return
        import importlib as _imp
        _rc = _imp.import_module("rich.console")
        _rj = _imp.import_module("rich.json")
        Console = _rc.Console
        JSON = _rj.JSON
        Console().print(JSON.from_data({"title": title, **payload}))
    except Exception:
        return


def _render_commit_panel(
    tool_name: str,
    project_label: str,
    agent_name: str,
    start_monotonic: float,
    end_monotonic: float,
    result_payload: dict[str, Any],
    created_iso: Optional[str],
) -> str | None:
    """Create the Rich panel text used for Git commit messages."""
    try:
        panel_ctx = rich_logger.ToolCallContext(
            tool_name=tool_name,
            args=[],
            kwargs={},
            project=project_label,
            agent=agent_name,
        )
        panel_ctx.start_time = start_monotonic
        panel_ctx.end_time = end_monotonic
        panel_ctx.success = True
        panel_ctx.result = result_payload
        if created_iso:
            parsed = _parse_iso(created_iso)
            if parsed:
                panel_ctx._created_at = parsed
        return rich_logger.render_tool_call_panel(panel_ctx)
    except Exception:
        return None

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
        "attachments_policy": getattr(agent, "attachments_policy", "auto"),
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


# --- Project sibling suggestion helpers -----------------------------------------------------

_PROJECT_PROFILE_FILENAMES: tuple[str, ...] = (
    "README.md",
    "Readme.md",
    "readme.md",
    "AGENTS.md",
    "CLAUDE.md",
    "Claude.md",
    "agents/README.md",
    "docs/README.md",
    "docs/overview.md",
)
_PROJECT_PROFILE_MAX_TOTAL_CHARS = 6000
_PROJECT_PROFILE_PER_FILE_CHARS = 1800
_PROJECT_SIBLING_REFRESH_TTL = timedelta(hours=12)
_PROJECT_SIBLING_REFRESH_LIMIT = 3
_PROJECT_SIBLING_MIN_SUGGESTION_SCORE = 0.92


def _canonical_project_pair(a_id: int, b_id: int) -> tuple[int, int]:
    if a_id == b_id:
        raise ValueError("Project pair must reference distinct projects.")
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


@asynccontextmanager
async def _archive_write_lock(archive: ProjectArchive, *, timeout_seconds: float = 60.0):
    try:
        async with archive_write_lock(archive, timeout_seconds=timeout_seconds):
            yield
    except TimeoutError as exc:
        raise ToolExecutionError(
            "ARCHIVE_LOCK_TIMEOUT",
            (
                f"Archive lock busy for project '{archive.slug}' at '{archive.lock_path}'. "
                f"Timed out after {timeout_seconds:.1f}s. "
                "Inspect running agents or call collect_lock_status to clear stale locks."
            ),
            recoverable=True,
            data={
                "project_slug": archive.slug,
                "lock_path": str(archive.lock_path),
                "timeout_seconds": timeout_seconds,
            },
        ) from exc


async def _read_file_preview(path: Path, *, max_chars: int) -> str:
    def _read() -> str:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                data = handle.read(max_chars + 1024)
        except Exception:
            return ""
        return (data or "").strip()[:max_chars]

    return await asyncio.to_thread(_read)


async def _build_project_profile(
    project: Project,
    agent_names: list[str],
) -> str:
    pieces: list[str] = [
        f"Identifier: {project.human_key}",
        f"Slug: {project.slug}",
        f"Agents: {', '.join(agent_names) if agent_names else 'None registered'}",
    ]

    base_path = Path(project.human_key)
    if base_path.exists():
        total_chars = 0
        seen_files: set[Path] = set()
        for rel_name in _PROJECT_PROFILE_FILENAMES:
            candidate = base_path / rel_name
            if candidate in seen_files or not candidate.exists() or not candidate.is_file():
                continue
            preview = await _read_file_preview(candidate, max_chars=_PROJECT_PROFILE_PER_FILE_CHARS)
            if not preview:
                continue
            pieces.append(f"===== {rel_name} =====\n{preview}")
            seen_files.add(candidate)
            total_chars += len(preview)
            if total_chars >= _PROJECT_PROFILE_MAX_TOTAL_CHARS:
                break
    return "\n\n".join(pieces)


def _heuristic_project_similarity(project_a: Project, project_b: Project) -> tuple[float, str]:
    # CRITICAL: Projects with identical human_key are the SAME project, not siblings
    # This should be filtered earlier, but adding safeguard here
    if project_a.human_key == project_b.human_key:
        return 0.0, "ERROR: Identical human_key - these are the SAME project, not siblings"

    slug_ratio = SequenceMatcher(None, project_a.slug, project_b.slug).ratio()
    human_ratio = SequenceMatcher(None, project_a.human_key, project_b.human_key).ratio()
    shared_prefix = 0.0
    try:
        prefix_a = Path(project_a.human_key).name.lower()
        prefix_b = Path(project_b.human_key).name.lower()
        shared_prefix = SequenceMatcher(None, prefix_a, prefix_b).ratio()
    except Exception:
        shared_prefix = 0.0

    score = max(slug_ratio, human_ratio, shared_prefix)
    reasons: list[str] = []
    if slug_ratio > 0.6:
        reasons.append(f"Slugs are similar ({slug_ratio:.2f})")
    if human_ratio > 0.6:
        reasons.append(f"Human keys align ({human_ratio:.2f})")
    parent_a = Path(project_a.human_key).parent
    parent_b = Path(project_b.human_key).parent
    if parent_a == parent_b:
        score = max(score, 0.85)
        reasons.append("Projects share the same parent directory")
    if not reasons:
        reasons.append("Heuristic comparison found limited overlap; treating as weak relation")
    return min(max(score, 0.0), 1.0), ", ".join(reasons)


async def _score_project_pair(
    project_a: Project,
    profile_a: str,
    project_b: Project,
    profile_b: str,
) -> tuple[float, str]:
    settings = get_settings()
    heuristic_score, heuristic_reason = _heuristic_project_similarity(project_a, project_b)

    if not settings.llm.enabled:
        return heuristic_score, heuristic_reason

    system_prompt = (
        "You are an expert analyst who maps whether two software projects are tightly related parts "
        "of the same overall product. Score relationship strength from 0.0 (unrelated) to 1.0 "
        "(same initiative with tightly coupled scope)."
    )
    user_prompt = (
        "Return strict JSON with keys: score (float 0-1), rationale (<=120 words).\n"
        "Focus on whether these projects represent collaborating slices of the same product.\n\n"
        f"Project A Profile:\n{profile_a}\n\nProject B Profile:\n{profile_b}"
    )

    try:
        completion = await complete_system_user(system_prompt, user_prompt, max_tokens=400)
        payload = completion.content.strip()
        data = json.loads(payload)
        score = float(data.get("score", heuristic_score))
        rationale = str(data.get("rationale", "")).strip() or heuristic_reason
        return min(max(score, 0.0), 1.0), rationale
    except Exception as exc:
        logger.debug("project_sibling.llm_failed", exc_info=exc)
        return heuristic_score, heuristic_reason + " (LLM fallback)"


async def refresh_project_sibling_suggestions(*, max_pairs: int = _PROJECT_SIBLING_REFRESH_LIMIT) -> None:
    await ensure_schema()
    async with get_session() as session:
        projects = (await session.execute(select(Project))).scalars().all()
        if len(projects) < 2:
            return

        agents_rows = await session.execute(select(Agent.project_id, Agent.name))
        agent_map: dict[int, list[str]] = defaultdict(list)
        for proj_id, name in agents_rows.fetchall():
            agent_map[int(proj_id)].append(name)

        existing_rows = (await session.execute(select(ProjectSiblingSuggestion))).scalars().all()
        existing_map: dict[tuple[int, int], ProjectSiblingSuggestion] = {}
        for suggestion in existing_rows:
            pair = _canonical_project_pair(suggestion.project_a_id, suggestion.project_b_id)
            existing_map[pair] = suggestion

        now = datetime.now(timezone.utc)
        to_evaluate: list[tuple[Project, Project, ProjectSiblingSuggestion | None]] = []
        for idx, project_a in enumerate(projects):
            if project_a.id is None:
                continue
            for project_b in projects[idx + 1 :]:
                if project_b.id is None:
                    continue

                # CRITICAL: Skip projects with identical human_key - they're the SAME project, not siblings
                # Two agents in /data/projects/smartedgar_mcp are on the SAME project
                # Siblings would be different directories like /data/projects/smartedgar_mcp_frontend
                if project_a.human_key == project_b.human_key:
                    continue

                pair = _canonical_project_pair(project_a.id, project_b.id)
                suggestion = existing_map.get(pair)
                if suggestion is None:
                    to_evaluate.append((project_a, project_b, None))
                else:
                    eval_ts = suggestion.evaluated_ts
                    # Normalize to timezone-aware UTC before arithmetic; SQLite may return naive datetimes
                    if eval_ts is not None:
                        if eval_ts.tzinfo is None or eval_ts.tzinfo.utcoffset(eval_ts) is None:
                            eval_ts = eval_ts.replace(tzinfo=timezone.utc)
                        else:
                            eval_ts = eval_ts.astimezone(timezone.utc)
                        age = now - eval_ts
                    else:
                        age = _PROJECT_SIBLING_REFRESH_TTL
                    if suggestion.status == "dismissed" and age < timedelta(days=7):
                        continue
                    if age >= _PROJECT_SIBLING_REFRESH_TTL and len(to_evaluate) < max_pairs:
                        to_evaluate.append((project_a, project_b, suggestion))
            if len(to_evaluate) >= max_pairs:
                break

        if not to_evaluate:
            return

        updated = False
        for project_a, project_b, suggestion in to_evaluate[:max_pairs]:
            profile_a = await _build_project_profile(project_a, agent_map.get(project_a.id or -1, []))
            profile_b = await _build_project_profile(project_b, agent_map.get(project_b.id or -1, []))
            score, rationale = await _score_project_pair(project_a, profile_a, project_b, profile_b)

            pair = _canonical_project_pair(project_a.id or 0, project_b.id or 0)
            record = existing_map.get(pair) if suggestion is None else suggestion
            if record is None:
                record = ProjectSiblingSuggestion(
                    project_a_id=pair[0],
                    project_b_id=pair[1],
                    score=score,
                    rationale=rationale,
                    status="suggested",
                )
                session.add(record)
                existing_map[pair] = record
            else:
                record.score = score
                record.rationale = rationale
                # Preserve user decisions
                if record.status not in {"confirmed", "dismissed"}:
                    record.status = "suggested"
            record.evaluated_ts = now
            updated = True

        if updated:
            await session.commit()


async def get_project_sibling_data() -> dict[int, dict[str, list[dict[str, Any]]]]:
    await ensure_schema()
    async with get_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT s.id, s.project_a_id, s.project_b_id, s.score, s.status, s.rationale,
                       s.evaluated_ts, pa.slug AS slug_a, pa.human_key AS human_a,
                       pb.slug AS slug_b, pb.human_key AS human_b
                FROM project_sibling_suggestions s
                JOIN projects pa ON pa.id = s.project_a_id
                JOIN projects pb ON pb.id = s.project_b_id
                ORDER BY s.score DESC
                """
            )
        )
        result_map: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"confirmed": [], "suggested": []})

        for row in rows.fetchall():
            suggestion_id = int(row[0])
            a_id = int(row[1])
            b_id = int(row[2])
            entry_base = {
                "suggestion_id": suggestion_id,
                "score": float(row[3] or 0.0),
                "status": row[4],
                "rationale": row[5] or "",
                "evaluated_ts": str(row[6]) if row[6] else None,
            }
            a_info = {"id": a_id, "slug": row[7], "human_key": row[8]}
            b_info = {"id": b_id, "slug": row[9], "human_key": row[10]}

            for current, other in ((a_info, b_info), (b_info, a_info)):
                bucket = result_map[current["id"]]
                entry = {**entry_base, "peer": other}
                if entry["status"] == "confirmed":
                    bucket["confirmed"].append(entry)
                elif entry["status"] != "dismissed" and float(entry_base["score"]) >= _PROJECT_SIBLING_MIN_SUGGESTION_SCORE:
                    bucket["suggested"].append(entry)

        return result_map


async def update_project_sibling_status(project_id: int, other_id: int, status: str) -> dict[str, Any]:
    normalized_status = status.lower()
    if normalized_status not in {"confirmed", "dismissed", "suggested"}:
        raise ValueError("Invalid status")

    await ensure_schema()
    async with get_session() as session:
        pair = _canonical_project_pair(project_id, other_id)
        suggestion = (
            await session.execute(
                select(ProjectSiblingSuggestion).where(
                    ProjectSiblingSuggestion.project_a_id == pair[0],
                    ProjectSiblingSuggestion.project_b_id == pair[1],
                )
            )
        ).scalars().first()

        if suggestion is None:
            # Create a baseline suggestion via refresh for this specific pair
            project_a_obj = await session.get(Project, pair[0])
            project_b_obj = await session.get(Project, pair[1])
            projects = [proj for proj in (project_a_obj, project_b_obj) if proj is not None]
            if len(projects) != 2:
                raise NoResultFound("Project pair not found")
            project_map = {proj.id: proj for proj in projects if proj.id is not None}
            agents_rows = await session.execute(
                select(Agent.project_id, Agent.name).where(
                    or_(Agent.project_id == pair[0], Agent.project_id == pair[1])
                )
            )
            agent_map: dict[int, list[str]] = defaultdict(list)
            for proj_id, name in agents_rows.fetchall():
                agent_map[int(proj_id)].append(name)
            profile_a = await _build_project_profile(project_map[pair[0]], agent_map.get(pair[0], []))
            profile_b = await _build_project_profile(project_map[pair[1]], agent_map.get(pair[1], []))
            score, rationale = await _score_project_pair(project_map[pair[0]], profile_a, project_map[pair[1]], profile_b)
            suggestion = ProjectSiblingSuggestion(
                project_a_id=pair[0],
                project_b_id=pair[1],
                score=score,
                rationale=rationale,
                status="suggested",
            )
            session.add(suggestion)
            await session.flush()

        now = datetime.now(timezone.utc)
        suggestion.status = normalized_status
        suggestion.evaluated_ts = now
        if normalized_status == "confirmed":
            suggestion.confirmed_ts = now
            suggestion.dismissed_ts = None
        elif normalized_status == "dismissed":
            suggestion.dismissed_ts = now
            suggestion.confirmed_ts = None

        await session.commit()

        project_a_obj = await session.get(Project, suggestion.project_a_id)
        project_b_obj = await session.get(Project, suggestion.project_b_id)
        project_lookup = {
            proj.id: proj
            for proj in (project_a_obj, project_b_obj)
            if proj is not None and proj.id is not None
        }

        def _project_payload(proj_id: int) -> dict[str, Any]:
            proj = project_lookup.get(proj_id)
            if proj is None:
                return {"id": proj_id, "slug": "", "human_key": ""}
            return {"id": proj.id, "slug": proj.slug, "human_key": proj.human_key}

        return {
            "id": suggestion.id,
            "status": suggestion.status,
            "score": suggestion.score,
            "rationale": suggestion.rationale,
            "project_a": _project_payload(suggestion.project_a_id),
            "project_b": _project_payload(suggestion.project_b_id),
            "evaluated_ts": str(suggestion.evaluated_ts) if suggestion.evaluated_ts else None,
        }


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

    mode = getattr(settings, "agent_name_enforcement_mode", "coerce").lower()
    if name_hint:
        sanitized = sanitize_agent_name(name_hint)
        if mode == "always_auto":
            sanitized = None
        if sanitized:
            if await available(sanitized):
                return sanitized
            if mode == "strict":
                raise ValueError(f"Agent name '{sanitized}' is already in use.")
        else:
            if mode == "strict":
                raise ValueError("Name hint must contain alphanumeric characters.")

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
    mode = getattr(settings, "agent_name_enforcement_mode", "coerce").lower()
    if mode == "always_auto" or name is None:
        desired_name = await _generate_unique_agent_name(project, settings, None)
    else:
        sanitized = sanitize_agent_name(name)
        if not sanitized:
            if mode == "strict":
                raise ValueError("Agent name must contain alphanumeric characters.")
            desired_name = await _generate_unique_agent_name(project, settings, None)
        else:
            desired_name = sanitized
    await ensure_schema()
    async with get_session() as session:
        # Use case-insensitive matching to be consistent with _agent_name_exists() and _get_agent()
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name) == desired_name.lower())
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
    async with _archive_write_lock(archive):
        await write_agent_profile(archive, _agent_to_dict(agent))
    return agent


async def _ensure_cross_project_link(
    source_project: Project,
    source_agent: Agent,
    target_project: Project,
    target_agent: Agent,
    *,
    ttl_seconds: int,
    reason: str = "auto-cross-project",
) -> AgentLink:
    """Ensure a directed AgentLink exists and is approved between two projects."""

    if any(item is None for item in (source_project.id, source_agent.id, target_project.id, target_agent.id)):
        raise ValueError("Projects and agents must be persisted before creating cross-project links.")

    await ensure_schema()
    now = datetime.now(timezone.utc)
    ttl = max(0, int(ttl_seconds or 0))
    expires = now + timedelta(seconds=max(60, ttl)) if ttl else None

    async with get_session() as session:
        stmt = select(AgentLink).where(
            AgentLink.a_project_id == source_project.id,
            AgentLink.a_agent_id == source_agent.id,
            AgentLink.b_project_id == target_project.id,
            AgentLink.b_agent_id == target_agent.id,
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing:
            updated = False
            if existing.status != "approved":
                existing.status = "approved"
                updated = True
            if expires and (existing.expires_ts is None or existing.expires_ts < expires):
                existing.expires_ts = expires
                updated = True
            if reason and reason not in (existing.reason or ""):
                existing.reason = reason
                updated = True
            if updated:
                existing.updated_ts = now
                session.add(existing)
                await session.commit()
                await session.refresh(existing)
            return existing

        link = AgentLink(
            a_project_id=source_project.id or 0,
            a_agent_id=source_agent.id or 0,
            b_project_id=target_project.id or 0,
            b_agent_id=target_agent.id or 0,
            status="approved",
            reason=reason,
            created_ts=now,
            updated_ts=now,
            expires_ts=expires,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return link


async def _lookup_agents_any_project(name: str) -> list[tuple[Project, Agent]]:
    """Return all (project, agent) pairs matching a given agent name globally."""

    target = (name or "").strip()
    if not target:
        return []
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Project, Agent)
            .join(Agent, Agent.project_id == Project.id)
            .where(func.lower(Agent.name) == target.lower())
        )
        return [(proj, agent) for proj, agent in result.all() if proj and agent]


async def _get_agent(project: Project, name: str) -> Agent:
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name) == name.lower())
        )
        agent = result.scalars().first()
        if not agent:
            raise NoResultFound(
                f"Agent '{name}' not registered for project '{project.human_key}'. "
                f"Tip: Use resource://agents/{project.slug} to discover registered agents."
            )
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


async def _create_file_reservation(
    project: Project,
    agent: Agent,
    path: str,
    exclusive: bool,
    reason: str,
    ttl_seconds: int,
) -> FileReservation:
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before creating file_reservations.")
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    await ensure_schema()
    async with get_session() as session:
        file_reservation = FileReservation(
            project_id=project.id,
            agent_id=agent.id,
            path_pattern=path,
            exclusive=exclusive,
            reason=reason,
            expires_ts=expires,
        )
        session.add(file_reservation)
        await session.commit()
        await session.refresh(file_reservation)
    return file_reservation


async def _collect_file_reservation_statuses(
    project: Project,
    *,
    include_released: bool = False,
    now: Optional[datetime] = None,
) -> list[FileReservationStatus]:
    if project.id is None:
        return []
    await ensure_schema()
    moment = now or datetime.now(timezone.utc)
    settings = get_settings()
    inactivity_seconds = max(0, int(settings.file_reservation_inactivity_seconds))
    activity_grace = max(0, int(settings.file_reservation_activity_grace_seconds))

    async with get_session() as session:
        stmt = (
            select(FileReservation, Agent)
            .join(Agent, FileReservation.agent_id == Agent.id)
            .where(FileReservation.project_id == project.id)
            .order_by(asc(FileReservation.created_ts))
        )
        if not include_released:
            stmt = stmt.where(cast(Any, FileReservation.released_ts).is_(None))
        result = await session.execute(stmt)
        rows = result.all()
        if not rows:
            return []
        agent_ids = [agent.id for _, agent in rows if agent.id is not None]
        send_map: dict[int, Optional[datetime]] = {}
        ack_map: dict[int, Optional[datetime]] = {}
        read_map: dict[int, Optional[datetime]] = {}
        if agent_ids:
            send_result = await session.execute(
                select(Message.sender_id, func.max(Message.created_ts))
                .where(
                    Message.project_id == project.id,
                    cast(Any, Message.sender_id).in_(agent_ids),
                )
                .group_by(Message.sender_id)
            )
            send_map = {row[0]: _ensure_utc(row[1]) for row in send_result}
            ack_result = await session.execute(
                select(MessageRecipient.agent_id, func.max(MessageRecipient.ack_ts))
                .join(Message, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project.id,
                    cast(Any, MessageRecipient.agent_id).in_(agent_ids),
                    cast(Any, MessageRecipient.ack_ts).is_not(None),
                )
                .group_by(MessageRecipient.agent_id)
            )
            ack_map = {row[0]: _ensure_utc(row[1]) for row in ack_result}
            read_result = await session.execute(
                select(MessageRecipient.agent_id, func.max(MessageRecipient.read_ts))
                .join(Message, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project.id,
                    cast(Any, MessageRecipient.agent_id).in_(agent_ids),
                    cast(Any, MessageRecipient.read_ts).is_not(None),
                )
                .group_by(MessageRecipient.agent_id)
            )
            read_map = {row[0]: _ensure_utc(row[1]) for row in read_result}

    workspace = _project_workspace_path(project)
    repo = _open_repo_if_available(workspace) if workspace is not None else None

    statuses: list[FileReservationStatus] = []
    for reservation, agent in rows:
        agent_id = agent.id or -1
        agent_last_active = _ensure_utc(agent.last_active_ts)
        last_mail = _max_datetime(send_map.get(agent_id), ack_map.get(agent_id), read_map.get(agent_id))

        matches: list[Path] = []
        fs_activity: Optional[datetime] = None
        git_activity: Optional[datetime] = None

        if workspace is not None:
            matches = _collect_matching_paths(workspace, reservation.path_pattern)
            if matches:
                fs_activity = _latest_filesystem_activity(matches)
                git_activity = _latest_git_activity(repo, matches)

        agent_inactive = (
            agent_last_active is None or (moment - agent_last_active).total_seconds() > inactivity_seconds
        )
        recent_mail = last_mail is not None and (moment - last_mail).total_seconds() <= activity_grace
        recent_fs = fs_activity is not None and (moment - fs_activity).total_seconds() <= activity_grace
        recent_git = git_activity is not None and (moment - git_activity).total_seconds() <= activity_grace

        stale = bool(
            reservation.released_ts is None
            and agent_inactive
            and not (recent_mail or recent_fs or recent_git)
        )
        reasons: list[str] = []
        if agent_inactive:
            reasons.append(f"agent_inactive>{inactivity_seconds}s")
        else:
            reasons.append("agent_recently_active")
        if recent_mail:
            reasons.append("mail_activity_recent")
        else:
            reasons.append(f"no_recent_mail_activity>{activity_grace}s")
        if matches:
            if recent_fs:
                reasons.append("filesystem_activity_recent")
            else:
                reasons.append(f"no_recent_filesystem_activity>{activity_grace}s")
            if recent_git:
                reasons.append("git_activity_recent")
            else:
                reasons.append(f"no_recent_git_activity>{activity_grace}s")
        else:
            reasons.append("path_pattern_unmatched")

        statuses.append(
            FileReservationStatus(
                reservation=reservation,
                agent=agent,
                stale=stale,
                stale_reasons=reasons,
                last_agent_activity=agent_last_active,
                last_mail_activity=last_mail,
                last_fs_activity=fs_activity,
                last_git_activity=git_activity,
            )
        )
    return statuses


async def _expire_stale_file_reservations(project_id: int) -> list[FileReservationStatus]:
    await ensure_schema()
    now = datetime.now(timezone.utc)

    project: Optional[Project] = None
    async with get_session() as session:
        project = await session.get(Project, project_id)
    if project is None:
        return []

    # Release any entries whose TTL has already elapsed
    async with get_session() as session:
        await session.execute(
            update(FileReservation)
            .where(
                FileReservation.project_id == project_id,
                cast(Any, FileReservation.released_ts).is_(None),
                FileReservation.expires_ts < now,
            )
            .values(released_ts=now)
        )
        await session.commit()

    statuses = await _collect_file_reservation_statuses(project, include_released=False, now=now)
    stale_statuses = [status for status in statuses if status.stale and status.reservation.id is not None]
    stale_ids = [cast(int, status.reservation.id) for status in stale_statuses]
    if not stale_ids:
        return []

    async with get_session() as session:
        await session.execute(
            update(FileReservation)
            .where(
                FileReservation.project_id == project_id,
                cast(Any, FileReservation.id).in_(stale_ids),
                cast(Any, FileReservation.released_ts).is_(None),
            )
            .values(released_ts=now)
        )
        await session.commit()

    for status in stale_statuses:
        status.reservation.released_ts = now
    return stale_statuses


def _file_reservations_conflict(existing: FileReservation, candidate_path: str, candidate_exclusive: bool, candidate_agent: Agent) -> bool:
    if existing.released_ts is not None:
        return False
    if existing.agent_id == candidate_agent.id:
        return False
    if not existing.exclusive and not candidate_exclusive:
        return False
    normalized_existing = existing.path_pattern
    # Treat simple directory patterns like "src/*" as inclusive of files under that directory
    # when comparing against concrete file paths like "src/app.py".
    def _expand_dir_star(p: str) -> str:
        if p.endswith("/*"):
            return p[:-1] + "*"  # "src/*" -> "src/**"-like breadth for fnmatchcase approximation
        return p
    a = _expand_dir_star(candidate_path)
    b = _expand_dir_star(normalized_existing)
    return (
        fnmatch.fnmatchcase(a, b)
        or fnmatch.fnmatchcase(b, a)
        or a == b
    )


def _patterns_overlap(a: str, b: str) -> bool:
    # Normalize simple relative prefixes for matching
    def _norm(s: str) -> str:
        while s.startswith("./"):
            s = s[2:]
        return s
    a1 = _norm(a)
    b1 = _norm(b)
    return (
        fnmatch.fnmatchcase(a1, b1)
        or fnmatch.fnmatchcase(b1, a1)
        or a1 == b1
    )


def _file_reservations_patterns_overlap(paths_a: Sequence[str], paths_b: Sequence[str]) -> bool:
    for pa in paths_a:
        for pb in paths_b:
            if _patterns_overlap(pa, pb):
                return True
    return False

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
            since_dt = _parse_iso(since_ts)
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


async def _list_outbox(
    project: Project,
    agent: Agent,
    limit: int,
    include_bodies: bool,
    since_ts: Optional[str],
) -> list[dict[str, Any]]:
    """List messages sent by the agent (their outbox)."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before listing outbox.")
    await ensure_schema()
    messages: list[dict[str, Any]] = []
    async with get_session() as session:
        stmt = (
            select(Message)
            .where(Message.project_id == project.id, Message.sender_id == agent.id)
            .order_by(desc(Message.created_ts))
            .limit(limit)
        )
        if since_ts:
            since_dt = _parse_iso(since_ts)
            if since_dt:
                stmt = stmt.where(Message.created_ts > since_dt)
        result = await session.execute(stmt)
        message_rows = result.scalars().all()

        # For each message, collect recipients grouped by kind
        for msg in message_rows:
            recs = await session.execute(
                select(MessageRecipient.kind, Agent.name)
                .join(Agent, MessageRecipient.agent_id == Agent.id)
                .where(MessageRecipient.message_id == msg.id)
            )
            to_list: list[str] = []
            cc_list: list[str] = []
            bcc_list: list[str] = []
            for kind, name in recs.all():
                if kind == "to":
                    to_list.append(name)
                elif kind == "cc":
                    cc_list.append(name)
                elif kind == "bcc":
                    bcc_list.append(name)
            payload = _message_to_dict(msg, include_body=include_bodies)
            payload["from"] = agent.name
            payload["to"] = to_list
            payload["cc"] = cc_list
            payload["bcc"] = bcc_list
            messages.append(payload)
    return messages


def _canonical_relpath_for_message(project: Project, message: Message, archive) -> str | None:
    """Resolve the canonical repo-relative path for a message markdown file.

    Supports both legacy filenames ("<id>.md") and the new descriptive pattern
    ("<ISO>__<subject-slug>__<id>.md"). Returns a path relative to the archive
    Git repo root, or None if no matching file is found.
    """
    ts = message.created_ts.astimezone(timezone.utc)
    y = ts.strftime("%Y")
    m = ts.strftime("%m")
    project_root = archive.root
    base_dir = project_root / "messages" / y / m
    id_str = str(message.id)

    candidates: list[Path] = []
    try:
        if base_dir.is_dir():
            # New filename pattern with ISO + subject slug + id suffix
            candidates.extend(base_dir.glob(f"*__*__{id_str}.md"))
            # Legacy filename pattern (id only)
            legacy = base_dir / f"{id_str}.md"
            if legacy.exists():
                candidates.append(legacy)
    except Exception:
        return None

    if not candidates:
        return None
    # Prefer lexicographically last (ISO prefix sorts ascending)
    selected = sorted(candidates)[-1]
    try:
        return selected.relative_to(archive.repo_root).as_posix()
    except Exception:
        return None


async def _commit_info_for_message(settings: Settings, project: Project, message: Message) -> dict[str, Any] | None:
    """Fetch commit metadata for the canonical message file (hexsha, summary, authored_ts, stats)."""
    archive = await ensure_archive(settings, project.slug)
    relpath = _canonical_relpath_for_message(project, message, archive)
    if not relpath:
        return None

    def _lookup():
        try:
            commit = next(archive.repo.iter_commits(paths=[relpath], max_count=1))
        except StopIteration:
            return None
        data: dict[str, Any] = {
            "hexsha": commit.hexsha[:12],
            "summary": commit.summary,
            "authored_ts": _iso(datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)),
        }
        try:
            stats = commit.stats.files.get(relpath, None)
            if stats:
                data["insertions"] = int(stats.get("insertions", 0))
                data["deletions"] = int(stats.get("deletions", 0))
        except Exception:
            pass
        # Attach concise diff summary (hunks count + first N +/- lines)
        try:
            parent = commit.parents[0] if commit.parents else None
            hunks = 0
            excerpt: list[str] = []
            if parent is not None:
                diffs = parent.diff(commit, paths=[relpath], create_patch=True)
                for d in diffs:
                    try:
                        patch = d.diff.decode("utf-8", "ignore")
                    except Exception:
                        patch = ""
                    for line in patch.splitlines():
                        if line.startswith("@@"):
                            hunks += 1
                        if line.startswith("+") or line.startswith("-"):
                            # skip file header lines like +++/---
                            if line.startswith("+++") or line.startswith("---"):
                                continue
                            excerpt.append(line[:200])
                            if len(excerpt) >= 12:
                                break
                    if len(excerpt) >= 12:
                        break
            data["diff_summary"] = {"hunks": hunks, "excerpt": excerpt}
        except Exception:
            pass
        return data

    return await asyncio.to_thread(_lookup)


def _summarize_messages(messages: Sequence[tuple[Message, str]]) -> dict[str, Any]:
    participants: set[str] = set()
    key_points: list[str] = []
    action_items: list[str] = []
    open_actions = 0
    done_actions = 0
    mentions: dict[str, int] = {}
    code_references: set[str] = set()
    keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")

    def _record_mentions(text: str) -> None:
        # very lightweight @mention parser
        for token in text.split():
            if token.startswith("@") and len(token) > 1:
                name = token[1:].strip(".,:;()[]{}")
                if name:
                    mentions[name] = mentions.get(name, 0) + 1

    def _maybe_code_ref(text: str) -> None:
        # capture backtick-enclosed references that look like files/paths
        start = 0
        while True:
            i = text.find("`", start)
            if i == -1:
                break
            j = text.find("`", i + 1)
            if j == -1:
                break
            snippet = text[i + 1 : j].strip()
            if ("/" in snippet or ".py" in snippet or ".ts" in snippet or ".md" in snippet) and (1 <= len(snippet) <= 120):
                code_references.add(snippet)
            start = j + 1

    for message, sender_name in messages:
        participants.add(sender_name)
        for line in message.body_md.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            _record_mentions(stripped)
            _maybe_code_ref(stripped)
            # bullet points and ordered lists → key points
            if stripped.startswith(('-', '*', '+')) or stripped[:2] in {"1.", "2.", "3.", "4.", "5."}:
                # normalize checkbox bullets to plain text for key points
                normalized = stripped
                if normalized.startswith(('- [ ]', '- [x]', '- [X]')):
                    normalized = normalized.split(']', 1)[-1].strip()
                key_points.append(normalized.lstrip("-+* "))
            # checkbox TODOs
            if stripped.startswith(('- [ ]', '* [ ]', '+ [ ]')):
                open_actions += 1
                action_items.append(stripped)
                continue
            if stripped.startswith(('- [x]', '- [X]', '* [x]', '* [X]', '+ [x]', '+ [X]')):
                done_actions += 1
                action_items.append(stripped)
                continue
            # keyword-based action detection
            upper = stripped.upper()
            if any(token in upper for token in keywords):
                action_items.append(stripped)

    # Sort mentions by frequency desc
    sorted_mentions = sorted(mentions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    summary: dict[str, Any] = {
        "participants": sorted(participants),
        "key_points": key_points[:10],
        "action_items": action_items[:10],
        "total_messages": len(messages),
        "open_actions": open_actions,
        "done_actions": done_actions,
        "mentions": [{"name": name, "count": count} for name, count in sorted_mentions],
    }
    if code_references:
        summary["code_references"] = sorted(code_references)[:10]
    return summary


async def _compute_thread_summary(
    project: Project,
    thread_id: str,
    include_examples: bool,
    llm_mode: bool,
    llm_model: Optional[str],
    *,
    per_thread_limit: Optional[int] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
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
        if per_thread_limit:
            stmt = stmt.limit(per_thread_limit)
        result = await session.execute(stmt)
        rows = result.all()
    summary = _summarize_messages(rows)

    if llm_mode and get_settings().llm.enabled:
        try:
            excerpts: list[str] = []
            for message, sender_name in rows[:15]:
                excerpts.append(f"- {sender_name}: {message.subject}\n{message.body_md[:800]}")
            if excerpts:
                system = (
                    "You are a senior engineer. Produce a concise JSON summary with keys: "
                    "participants[], key_points[], action_items[], mentions[{name,count}], code_references[], "
                    "total_messages, open_actions, done_actions. Derive from the given thread excerpts."
                )
                user = "\n\n".join(excerpts)
                llm_resp = await complete_system_user(system, user, model=llm_model)
                parsed = _parse_json_safely(llm_resp.content)
                if parsed:
                    for key in (
                        "participants",
                        "key_points",
                        "action_items",
                        "mentions",
                        "code_references",
                        "total_messages",
                        "open_actions",
                        "done_actions",
                    ):
                        value = parsed.get(key)
                        if value:
                            summary[key] = value
        except Exception as e:
            logger.debug("thread_summary.llm_skipped", extra={"thread_id": thread_id, "error": str(e)})

    examples: list[dict[str, Any]] = []
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
    return summary, examples, len(rows)


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
        # Read current value first
        result_sel = await session.execute(
            select(MessageRecipient).where(
                MessageRecipient.message_id == message_id,
                MessageRecipient.agent_id == agent.id,
            )
        )
        rec = result_sel.scalars().first()
        if not rec:
            return None
        current: Optional[datetime] = getattr(rec, field, None)
        if current is not None:
            # Already set; return existing value without updating
            return current
        # Set only if null
        stmt = (
            update(MessageRecipient)
            .where(MessageRecipient.message_id == message_id, MessageRecipient.agent_id == agent.id)
            .values({field: now})
        )
        await session.execute(stmt)
        await session.commit()
    return now


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
        tool_name: str,
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
        # Re-fetch settings at call time so tests that mutate env + clear cache take effect
        settings = get_settings()
        call_start = time.perf_counter()
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
        # Respect agent-level attachments policy override if set
        embed_policy: str = "auto"
        if getattr(sender, "attachments_policy", None) in {"inline", "file"}:
            convert_markdown = True
            embed_policy = sender.attachments_policy

        payload: dict[str, Any] | None = None

        async with _archive_write_lock(archive):
            # Server-side file_reservations enforcement: block if conflicting active exclusive file_reservation exists
            if settings.file_reservations_enforcement_enabled:
                await _expire_stale_file_reservations(project.id or 0)
                now_ts = datetime.now(timezone.utc)
                y_dir = now_ts.strftime("%Y")
                m_dir = now_ts.strftime("%m")
                candidate_surfaces: list[str] = []
                candidate_surfaces.append(f"agents/{sender.name}/outbox/{y_dir}/{m_dir}/*.md")
                for r in to_agents + cc_agents + bcc_agents:
                    candidate_surfaces.append(f"agents/{r.name}/inbox/{y_dir}/{m_dir}/*.md")

                async with get_session() as session:
                    rows = await session.execute(
                        select(FileReservation, Agent.name)
                        .join(Agent, FileReservation.agent_id == Agent.id)
                        .where(
                            FileReservation.project_id == project.id,
                            cast(Any, FileReservation.released_ts).is_(None),
                            FileReservation.expires_ts > now_ts,
                        )
                    )
                    active_file_reservations = rows.all()

                conflicts: list[dict[str, Any]] = []
                for surface in candidate_surfaces:
                    for file_reservation_record, holder_name in active_file_reservations:
                        if _file_reservations_conflict(file_reservation_record, surface, True, sender):
                            conflicts.append({
                                "surface": surface,
                                "holder": holder_name,
                                "path_pattern": file_reservation_record.path_pattern,
                                "exclusive": file_reservation_record.exclusive,
                                "expires_ts": _iso(file_reservation_record.expires_ts),
                            })
                if conflicts:
                    # Return a structured error payload that clients can surface directly
                    return {
                        "error": {
                            "type": "FILE_RESERVATION_CONFLICT",
                            "message": "Conflicting active file_reservations prevent message write.",
                            "conflicts": conflicts,
                        }
                    }

            processed_body, attachments_meta, attachment_files = await process_attachments(
                archive,
                body_md,
                attachment_paths or [],
                convert_markdown,
                embed_policy=embed_policy,
            )
            # Fallback: if body contains inline data URI, reflect that in attachments meta for API parity
            if not attachments_meta and ("data:image" in body_md):
                attachments_meta.append({"type": "inline", "media_type": "image/webp"})
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
            result_snapshot: dict[str, Any] = {
                "deliveries": [
                    {
                        "project": project.human_key,
                        "payload": payload,
                    }
                ],
                "count": 1,
            }
            panel_end = time.perf_counter()
            commit_panel_text = _render_commit_panel(
                tool_name,
                project.human_key,
                sender.name,
                call_start,
                panel_end,
                result_snapshot,
                frontmatter.get("created"),
            )
            await write_message_bundle(
                archive,
                frontmatter,
                processed_body,
                sender.name,
                recipients_for_archive,
                attachment_files,
                commit_panel_text,
            )
        await ctx.info(
            f"Message {message.id} created by {sender.name} (to {', '.join(recipients_for_archive)})"
        )
        if payload is None:
            raise RuntimeError("Message payload was not generated.")
        return payload

    @mcp.tool(name="health_check", description="Return basic readiness information for the Agent Mail server.")
    @_instrument_tool("health_check", cluster=CLUSTER_SETUP, capabilities={"infrastructure"}, complexity="low")
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
    @_instrument_tool("ensure_project", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "storage"}, complexity="low", project_arg="human_key")
    async def ensure_project(ctx: Context, human_key: str) -> dict[str, Any]:
        """
        Idempotently create or ensure a project exists for the given human key.

        When to use
        -----------
        - First call in a workflow targeting a new repo/path identifier.
        - As a guard before registering agents or sending messages.

        How it works
        ------------
        - Validates that `human_key` is an absolute directory path (the agent's working directory).
        - Computes a stable slug from `human_key` (lowercased, safe characters) so
          multiple agents can refer to the same project consistently.
        - Ensures DB row exists and that the on-disk archive is initialized
          (e.g., `messages/`, `agents/`, `file_reservations/` directories).

        CRITICAL: Project Identity Rules
        ---------------------------------
        - The `human_key` MUST be the absolute path to the agent's working directory
        - Two agents working in the SAME directory path are working on the SAME project
        - Example: Both agents in /data/projects/smartedgar_mcp → SAME project
        - Sibling projects are DIFFERENT directories (e.g., /data/projects/smartedgar_mcp
          vs /data/projects/smartedgar_mcp_frontend)

        Parameters
        ----------
        human_key : str
            The absolute path to the agent's working directory (e.g., "/data/projects/backend").
            This MUST be an absolute path, not a relative path or arbitrary slug.
            This is the canonical identifier for the project - all agents working in this
            directory will share the same project identity.

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
          "params": {"name": "ensure_project", "arguments": {"human_key": "/data/projects/backend"}}
        }
        ```

        Common mistakes
        ---------------
        - Passing a relative path (e.g., "./backend") instead of an absolute path
        - Using arbitrary slugs instead of the actual working directory path
        - Creating separate projects for the same directory with different slugs

        Idempotency
        -----------
        - Safe to call multiple times. If the project already exists, the existing
          record is returned and the archive is ensured on disk (no destructive changes).
        """
        # Validate that human_key is an absolute path (cross-platform)
        if not Path(human_key).is_absolute():
            raise ValueError(
                f"human_key must be an absolute directory path, got: '{human_key}'. "
                "Use the agent's working directory path (e.g., '/data/projects/backend' on Unix "
                "or 'C:\\projects\\backend' on Windows)."
            )

        await ctx.info(f"Ensuring project for key '{human_key}'.")
        project = await _ensure_project(human_key)
        await ensure_archive(settings, project.slug)
        return _project_to_dict(project)

    @mcp.tool(name="register_agent")
    @_instrument_tool("register_agent", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="name", project_arg="project_key")
    async def register_agent(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name: Optional[str] = None,
        task_description: str = "",
        attachments_policy: str = "auto",
    ) -> dict[str, Any]:
        """
        Create or update an agent identity within a project and persist its profile to Git.

        When to use
        -----------
        - At the start of a coding session by any automated agent.
        - To update an existing agent's program/model/task metadata and bump last_active.

        Semantics
        ---------
        - If `name` is omitted, a random adjective+noun name is auto-generated.
        - Reusing the same `name` updates the profile (program/model/task) and
          refreshes `last_active_ts`.
        - A `profile.json` file is written under `agents/<Name>/` in the project archive.

        CRITICAL: Agent Naming Rules
        -----------------------------
        - Agent names MUST be randomly generated adjective+noun combinations
        - Examples: "GreenLake", "BlueDog", "RedStone", "PurpleBear"
        - Names should be unique, easy to remember, and NOT descriptive
        - INVALID examples: "BackendHarmonizer", "DatabaseMigrator", "UIRefactorer"
        - The whole point: names should be memorable identifiers, not role descriptions
        - Best practice: Omit the `name` parameter to auto-generate a valid name

        Parameters
        ----------
        project_key : str
            The same human key you passed to `ensure_project` (or equivalent identifier).
        program : str
            The agent program (e.g., "codex-cli", "claude-code").
        model : str
            The underlying model (e.g., "gpt5-codex", "opus-4.1").
        name : Optional[str]
            MUST be a valid adjective+noun combination if provided (e.g., "BlueLake").
            If omitted, a random valid name is auto-generated (RECOMMENDED).
            Names are unique per project; passing the same name updates the profile.
        task_description : str
            Short description of current focus (shows up in directory listings).

        Returns
        -------
        dict
            { id, name, program, model, task_description, inception_ts, last_active_ts, project_id }

        Examples
        --------
        Register with auto-generated name (RECOMMENDED):
        ```json
        {"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"register_agent","arguments":{
          "project_key":"/data/projects/backend","program":"codex-cli","model":"gpt5-codex","task_description":"Auth refactor"
        }}}
        ```

        Register with explicit valid name:
        ```json
        {"jsonrpc":"2.0","id":"4","method":"tools/call","params":{"name":"register_agent","arguments":{
          "project_key":"/data/projects/backend","program":"claude-code","model":"opus-4.1","name":"BlueLake","task_description":"Navbar redesign"
        }}}
        ```

        Pitfalls
        --------
        - Names MUST match the adjective+noun format or an error will be raised
        - Names are case-insensitive unique. If you see "already in use", pick another or omit `name`.
        - Use the same `project_key` consistently across cooperating agents.
        """
        project = await _get_project_by_identifier(project_key)
        if settings.tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                c = Console()
                c.print(Panel(f"project=[bold]{project.human_key}[/]\nname=[bold]{name or '(generated)'}[/]\nprogram={program}\nmodel={model}", title="tool: register_agent", border_style="green"))
            except Exception:
                pass
        # sanitize attachments policy
        ap = (attachments_policy or "auto").lower()
        if ap not in {"auto", "inline", "file"}:
            ap = "auto"
        agent = await _get_or_create_agent(project, name, program, model, task_description, settings)
        # Persist attachment policy if changed
        if getattr(agent, "attachments_policy", None) != ap:
            async with get_session() as session:
                db_agent = await session.get(Agent, agent.id)
                if db_agent:
                    db_agent.attachments_policy = ap
                    session.add(db_agent)
                    await session.commit()
                    await session.refresh(db_agent)
                    agent = db_agent
        await ctx.info(f"Registered agent '{agent.name}' for project '{project.human_key}'.")
        return _agent_to_dict(agent)

    @mcp.tool(name="whois")
    @_instrument_tool("whois", cluster=CLUSTER_IDENTITY, capabilities={"identity", "audit"}, project_arg="project_key", agent_arg="agent_name")
    async def whois(
        ctx: Context,
        project_key: str,
        agent_name: str,
        include_recent_commits: bool = True,
        commit_limit: int = 5,
    ) -> dict[str, Any]:
        """
        Return enriched profile details for an agent, optionally including recent archive commits.

        Discovery
        ---------
        To discover available agent names, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        agent_name : str
            Agent name to look up (use resource://agents/{project_key} to discover names).
        include_recent_commits : bool
            If true, include latest commits touching the project archive authored by the configured git author.
        commit_limit : int
            Maximum number of recent commits to include.

        Returns
        -------
        dict
            Agent profile augmented with { recent_commits: [{hexsha, summary, authored_ts}] } when requested.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        profile = _agent_to_dict(agent)
        recent: list[dict[str, Any]] = []
        if include_recent_commits:
            archive = await ensure_archive(settings, project.slug)
            repo: Repo = archive.repo
            try:
                # Limit to archive path; extract last commits
                count = max(1, min(50, commit_limit))
                for commit in repo.iter_commits(paths=["."], max_count=count):
                    recent.append(
                        {
                            "hexsha": commit.hexsha[:12],
                            "summary": commit.summary,
                            "authored_ts": _iso(datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)),
                        }
                    )
            except Exception:
                pass
        profile["recent_commits"] = recent
        await ctx.info(f"whois for '{agent_name}' in '{project.human_key}' returned {len(recent)} commits")
        return profile

    @mcp.tool(name="create_agent_identity")
    @_instrument_tool("create_agent_identity", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="name_hint", project_arg="project_key")
    async def create_agent_identity(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name_hint: Optional[str] = None,
        task_description: str = "",
        attachments_policy: str = "auto",
    ) -> dict[str, Any]:
        """
        Create a new, unique agent identity and persist its profile to Git.

        How this differs from `register_agent`
        --------------------------------------
        - Always creates a new identity with a fresh unique name (never updates an existing one).
        - `name_hint`, if provided, MUST be a valid adjective+noun combination and must be available,
          otherwise an error is raised. Without a hint, a random adjective+noun name is generated.

        CRITICAL: Agent Naming Rules
        -----------------------------
        - Agent names MUST be randomly generated adjective+noun combinations
        - Examples: "GreenCastle", "BlueLake", "RedStone", "PurpleBear"
        - Names should be unique, easy to remember, and NOT descriptive
        - INVALID examples: "BackendHarmonizer", "DatabaseMigrator", "UIRefactorer"
        - Best practice: Omit `name_hint` to auto-generate a valid name (RECOMMENDED)

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
        Auto-generate name (RECOMMENDED):
        ```json
        {"jsonrpc":"2.0","id":"c2","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/data/projects/backend","program":"claude-code","model":"opus-4.1"
        }}}
        ```

        With valid name hint:
        ```json
        {"jsonrpc":"2.0","id":"c1","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/data/projects/backend","program":"codex-cli","model":"gpt5-codex","name_hint":"GreenCastle",
          "task_description":"DB migration spike"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        unique_name = await _generate_unique_agent_name(project, settings, name_hint)
        ap = (attachments_policy or "auto").lower()
        if ap not in {"auto", "inline", "file"}:
            ap = "auto"
        agent = await _create_agent_record(project, unique_name, program, model, task_description)
        # Update attachments policy immediately
        async with get_session() as session:
            db_agent = await session.get(Agent, agent.id)
            if db_agent:
                db_agent.attachments_policy = ap
                session.add(db_agent)
                await session.commit()
                await session.refresh(db_agent)
                agent = db_agent
        archive = await ensure_archive(settings, project.slug)
        async with _archive_write_lock(archive):
            await write_agent_profile(archive, _agent_to_dict(agent))
        await ctx.info(f"Created new agent identity '{agent.name}' for project '{project.human_key}'.")
        return _agent_to_dict(agent)

    @mcp.tool(name="send_message")
    @_instrument_tool(
        "send_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "write"},
        project_arg="project_key",
        agent_arg="sender_name",
    )
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
        auto_contact_if_blocked: bool = False,
    ) -> dict[str, Any]:
        """
        Send a Markdown message to one or more recipients and persist canonical and mailbox copies to Git.

        Discovery
        ---------
        To discover available agent names for recipients, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

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
            {
              "deliveries": [ { "project": str, "payload": { ... message payload ... } } ],
              "count": int
            }

        Edge cases
        ----------
        - If no recipients are given, the call fails.
        - Unknown recipient names fail fast; register them first.
        - Non-absolute attachment paths are resolved relative to the project archive root.

        Do / Don't
        ----------
        Do:
        - Keep subjects concise and specific (aim for ≤ 80 characters).
        - Use `thread_id` (or `reply_message`) to keep related discussion in a single thread.
        - Address only relevant recipients; use CC/BCC sparingly and intentionally.
        - Prefer Markdown links; attach images only when they materially aid understanding. The server
          auto-converts images to WebP and may inline small images depending on policy.

        Don't:
        - Send large, repeated binaries—reuse prior attachments via `attachment_paths` when possible.
        - Change topics mid-thread—start a new thread for a new subject.
        - Broadcast to "all" agents unnecessarily—target just the agents who need to act.

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
        # Normalize cc/bcc inputs and validate types for friendlier UX
        if isinstance(cc, str):
            cc = [cc]
        if isinstance(bcc, str):
            bcc = [bcc]
        if cc is not None and not isinstance(cc, list):
            await ctx.error("INVALID_ARGUMENT: cc must be a list of strings or a single string.")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "cc must be a list of strings or a single string.",
                recoverable=True,
                data={"argument": "cc"},
            )
        if bcc is not None and not isinstance(bcc, list):
            await ctx.error("INVALID_ARGUMENT: bcc must be a list of strings or a single string.")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "bcc must be a list of strings or a single string.",
                recoverable=True,
                data={"argument": "bcc"},
            )
        if cc is not None and any(not isinstance(x, str) for x in cc):
            await ctx.error("INVALID_ARGUMENT: cc items must be strings (agent names).")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "cc items must be strings (agent names).",
                recoverable=True,
                data={"argument": "cc"},
            )
        if bcc is not None and any(not isinstance(x, str) for x in bcc):
            await ctx.error("INVALID_ARGUMENT: bcc items must be strings (agent names).")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "bcc items must be strings (agent names).",
                recoverable=True,
                data={"argument": "bcc"},
            )
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                _rt = _imp.import_module("rich.text")
                Console = _rc.Console
                Panel = _rp.Panel
                Text = _rt.Text
                c = Console()
                title = f"tool: send_message — to={len(to)} cc={len(cc or [])} bcc={len(bcc or [])}"
                body = Text.assemble(
                    ("project: ", "cyan"), (project.human_key, "white"), "\n",
                    ("sender: ", "cyan"), (sender_name, "white"), "\n",
                    ("subject: ", "cyan"), (subject[:120], "white"),
                )
                c.print(Panel(body, title=title, border_style="green"))
            except Exception:
                pass
        sender = await _get_agent(project, sender_name)
        # Enforce contact policies (per-recipient) with auto-allow heuristics
        settings_local = get_settings()
        # Allow ack-required messages to bypass contact enforcement entirely
        if settings_local.contact_enforcement_enabled and not ack_required:
            # allow replies always; if thread present and recipient already on thread, allow
            auto_ok_names: set[str] = set()
            if thread_id:
                try:
                    thread_rows: list[tuple[Message, str]]
                    sender_alias = aliased(Agent)
                    # Build criteria: thread_id match or numeric id seed
                    criteria = [Message.thread_id == thread_id]
                    try:
                        seed_id = int(thread_id)
                        criteria.append(Message.id == seed_id)
                    except Exception:
                        pass
                    async with get_session() as s:
                        stmt = (
                            select(Message, sender_alias.name)
                            .join(sender_alias, Message.sender_id == sender_alias.id)
                            .where(Message.project_id == project.id, or_(*criteria))
                            .limit(500)
                        )
                        thread_rows = list((await s.execute(stmt)).all())
                    # collect participants (sender names and recipients)
                    participants: set[str] = {n for _m, n in thread_rows}
                    auto_ok_names.update(participants)
                except Exception:
                    pass
            # allow recent overlapping file_reservations contact (shared surfaces) by default
            # best-effort: if both agents hold any file_reservation currently active, auto allow
            now_utc = datetime.now(timezone.utc)
            try:
                async with get_session() as s2:
                    file_reservation_rows = await s2.execute(
                        select(FileReservation, Agent.name)
                        .join(Agent, FileReservation.agent_id == Agent.id)
                        .where(FileReservation.project_id == project.id, cast(Any, FileReservation.released_ts).is_(None), FileReservation.expires_ts > now_utc)
                    )
                    name_to_file_reservations: dict[str, list[str]] = {}
                    for c, nm in file_reservation_rows.all():
                        name_to_file_reservations.setdefault(nm, []).append(c.path_pattern)
                sender_file_reservations = name_to_file_reservations.get(sender.name, [])
                for nm in to + (cc or []) + (bcc or []):
                    # Always allow self-messages
                    if nm == sender.name:
                        continue
                    their = name_to_file_reservations.get(nm, [])
                    if sender_file_reservations and their and _file_reservations_patterns_overlap(sender_file_reservations, their):
                        auto_ok_names.add(nm)
            except Exception:
                pass
            # For each recipient, require link unless policy/open or in auto_ok
            blocked_recipients: list[str] = []
            async with get_session() as s3:
                for nm in to + (cc or []) + (bcc or []):
                    if nm in auto_ok_names:
                        continue
                    # recipient lookup
                    try:
                        rec = await _get_agent(project, nm)
                    except Exception:
                        continue
                    rec_policy = getattr(rec, "contact_policy", "auto").lower()
                    # allow self always
                    if rec.name == sender.name:
                        continue
                    if rec_policy == "open":
                        continue
                    if rec_policy == "block_all":
                        await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                        raise ToolExecutionError(
                            "CONTACT_BLOCKED",
                            "Recipient is not accepting messages.",
                            recoverable=True,
                        )
                    # contacts_only or auto -> must have approved link or prior contact within TTL
                    ttl = timedelta(seconds=int(settings_local.contact_auto_ttl_seconds))
                    recent_ok = False
                    try:
                        # check any message between these two within TTL
                        since_dt = now_utc - ttl
                        q = text(
                            """
                            SELECT 1 FROM messages m
                            WHERE m.project_id = :pid
                              AND m.created_ts > :since
                              AND (
                                   (m.sender_id = :sid AND EXISTS (SELECT 1 FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id WHERE mr.message_id=m.id AND a.name = :rname))
                                   OR
                                   (EXISTS (SELECT 1 FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id WHERE mr.message_id=m.id AND a.name = :sname) AND m.sender_id = (SELECT id FROM agents WHERE project_id=:pid AND name=:rname))
                              )
                            LIMIT 1
                            """
                        )
                        row = await s3.execute(q, {"pid": project.id, "since": since_dt, "sid": sender.id, "sname": sender.name, "rname": rec.name})
                        recent_ok = row.first() is not None
                    except Exception:
                        recent_ok = False
                    if rec_policy == "auto" and recent_ok:
                        continue
                    # check approved AgentLink (local project)
                    try:
                        link = await s3.execute(
                            select(AgentLink)
                            .where(
                                AgentLink.a_project_id == project.id,
                                AgentLink.a_agent_id == sender.id,
                                AgentLink.b_project_id == project.id,
                                AgentLink.b_agent_id == rec.id,
                                AgentLink.status == "approved",
                            )
                            .limit(1)
                        )
                        if link.first() is not None:
                            continue
                    except Exception:
                        pass
                    # If message requires acknowledgement and recipient is local, allow to proceed without a link
                    if ack_required:
                        continue
                    blocked_recipients.append(rec.name)

            if blocked_recipients:
                remedies = [
                    "Call request_contact(project_key, from_agent, to_agent) to request approval",
                    "Call macro_contact_handshake(project_key, requester, target, auto_accept=true) to automate",
                ]
                attempted: list[str] = []
                # Respect explicit flag or server default ergonomics
                effective_auto_contact: bool = bool(auto_contact_if_blocked or getattr(settings_local, "messaging_auto_handshake_on_block", True))
                if effective_auto_contact:
                    try:
                        from fastmcp.tools.tool import FunctionTool  # type: ignore
                        # Prefer a single handshake with auto_accept=true
                        handshake = cast(FunctionTool, cast(Any, macro_contact_handshake))
                        for nm in blocked_recipients:
                            try:
                                await handshake.run({
                                    "project_key": project.human_key,
                                    "requester": sender.name,
                                    "target": nm,
                                    "reason": "auto-handshake by send_message",
                                    "auto_accept": True,
                                    "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                })
                                attempted.append(nm)
                            except Exception:
                                pass

                        # If auto-retry is enabled and at least one handshake happened, re-evaluate recipients once
                        if settings_local.contact_auto_retry_enabled and attempted:
                            blocked_recipients = []
                            async with get_session() as s3b:
                                for nm in to + (cc or []) + (bcc or []):
                                    try:
                                        rec = await _get_agent(project, nm)
                                    except Exception:
                                        continue
                                    if rec.name == sender.name:
                                        continue
                                    rec_policy = getattr(rec, "contact_policy", "auto").lower()
                                    if rec_policy == "open":
                                        continue
                                    # After auto-approval, link should exist; double-check
                                    link = await s3b.execute(
                                        select(AgentLink)
                                        .where(
                                            AgentLink.a_project_id == project.id,
                                            AgentLink.a_agent_id == sender.id,
                                            AgentLink.b_project_id == project.id,
                                            AgentLink.b_agent_id == rec.id,
                                            AgentLink.status == "approved",
                                        )
                                        .limit(1)
                                    )
                                    if link.first() is None and not ack_required:
                                        blocked_recipients.append(rec.name)
                    except Exception:
                        pass
                if blocked_recipients:
                    err_type: str = "CONTACT_REQUIRED"
                    blocked_sorted = sorted(set(blocked_recipients))
                    recipient_list = ", ".join(blocked_sorted)
                    sample_target = blocked_sorted[0]
                    project_expr = repr(project.human_key)
                    sender_expr = repr(sender.name)
                    target_expr = repr(sample_target)
                    err_msg_parts = [
                        f"Contact approval required for recipients: {recipient_list}.",
                        (
                            "Before retrying, request approval with "
                            f"`request_contact(project_key={project_expr}, from_agent={sender_expr}, "
                            f"to_agent={target_expr})` or run "
                            f"`macro_contact_handshake(project_key={project_expr}, requester={sender_expr}, "
                            f"target={target_expr}, auto_accept=True)`."
                        ),
                        "Alternatively, send your message inside a recent thread that already includes them by reusing its thread_id.",
                    ]
                    if attempted:
                        err_msg_parts.append(
                            f"Automatic handshake attempts already ran for: {', '.join(attempted)}; wait for approval or retry the suggested calls explicitly."
                        )
                    err_msg: str = " ".join(err_msg_parts)
                    err_data: dict[str, Any] = {
                        "recipients_blocked": sorted(set(blocked_recipients)),
                        "remedies": remedies,
                        "auto_contact_attempted": attempted,
                    }
                    # Provide actionable sample calls
                    try:
                        if blocked_recipients:
                            examples: list[dict[str, Any]] = []
                            # Show a macro example for the first blocked recipient
                            examples.append(
                                {
                                    "tool": "macro_contact_handshake",
                                    "arguments": {
                                        "project_key": project.human_key,
                                        "requester": sender.name,
                                        "target": blocked_recipients[0],
                                        "auto_accept": True,
                                        "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                    },
                                }
                            )
                            # Also include direct request_contact examples
                            for nm in blocked_recipients[:3]:
                                examples.append(
                                    {
                                        "tool": "request_contact",
                                        "arguments": {
                                            "project_key": project.human_key,
                                            "from_agent": sender.name,
                                            "to_agent": nm,
                                            "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                        },
                                    }
                                )
                            err_data["suggested_tool_calls"] = examples
                    except Exception:
                        pass
                    await ctx.error(f"{err_type}: {err_msg}")
                    raise ToolExecutionError(
                        err_type,
                        err_msg,
                        recoverable=True,
                        data=err_data,
                    )
        # Split recipients into local vs external (approved links)
        local_to: list[str] = []
        local_cc: list[str] = []
        local_bcc: list[str] = []
        external: dict[int, dict[str, Any]] = {}

        async with get_session() as sx:
            # Preload local agent names (normalized -> canonical stored name)
            existing = await sx.execute(select(Agent.name).where(Agent.project_id == project.id))
            local_lookup: dict[str, str] = {}
            for row in existing.fetchall():
                canonical_name = (row[0] or "").strip()
                if not canonical_name:
                    continue
                sanitized_canonical = sanitize_agent_name(canonical_name) or canonical_name
                for key in {canonical_name.lower(), sanitized_canonical.lower()}:
                    local_lookup.setdefault(key, canonical_name)

            sender_candidate_keys = {
                key.lower()
                for key in (
                    (sender.name or "").strip(),
                    sanitize_agent_name(sender.name or "") or "",
                )
                if key
            }

            def _normalize(value: str) -> tuple[str, set[str], Optional[str]]:
                """Trim input, derive comparable lowercase keys, and canonical lookup token."""
                trimmed = (value or "").strip()
                sanitized = sanitize_agent_name(trimmed)
                keys: set[str] = set()
                if trimmed:
                    keys.add(trimmed.lower())
                if sanitized:
                    keys.add(sanitized.lower())
                canonical = sanitized or (trimmed if trimmed else None)
                return trimmed or value, keys, canonical

            unknown_local: set[str] = set()
            unknown_external: dict[str, list[str]] = defaultdict(list)

            class _ContactBlocked(Exception):
                pass

            async def _route(name_list: list[str], kind: str) -> None:
                for raw in name_list:
                    candidate = raw or ""
                    explicit_override = False
                    target_project_override: Project | None = None
                    target_project_label: str | None = None
                    agent_fragment = candidate

                    # Explicit external addressing: project:<slug-or-key>#<AgentName>
                    if candidate.startswith("project:") and "#" in candidate:
                        explicit_override = True
                        try:
                            _, rest = candidate.split(":", 1)
                            slug_part, agent_part = rest.split("#", 1)
                            target_project_override = await _get_project_by_identifier(slug_part.strip())
                            target_project_label = target_project_override.human_key or target_project_override.slug
                            agent_fragment = agent_part
                        except Exception:
                            label = slug_part.strip() if "slug_part" in locals() and slug_part.strip() else "(invalid project)"
                            unknown_external[label].append(candidate.strip() or candidate)
                            continue

                    # Alternate explicit format: <AgentName>@<project-identifier>
                    if not explicit_override and "@" in candidate:
                        name_part, project_part = candidate.split("@", 1)
                        if name_part.strip() and project_part.strip():
                            try:
                                target_project_override = await _get_project_by_identifier(project_part.strip())
                                target_project_label = target_project_override.human_key or target_project_override.slug
                                agent_fragment = name_part
                                explicit_override = True
                            except Exception:
                                label = project_part.strip() or "(invalid project)"
                                unknown_external[label].append(candidate.strip() or candidate)
                                continue

                    display_value, key_candidates, canonical = _normalize(agent_fragment)
                    if not key_candidates or not canonical:
                        if explicit_override:
                            label = target_project_label or "(unknown project)"
                            unknown_external[label].append(candidate.strip() or candidate)
                        else:
                            unknown_local.add(candidate.strip() or candidate)
                        continue

                    # Always allow self-send (local context only)
                    if not explicit_override and sender_candidate_keys.intersection(key_candidates):
                        if kind == "to":
                            local_to.append(sender.name)
                        elif kind == "cc":
                            local_cc.append(sender.name)
                        else:
                            local_bcc.append(sender.name)
                        continue

                    if not explicit_override:
                        resolved_local = None
                        for key in key_candidates:
                            resolved_local = local_lookup.get(key)
                            if resolved_local:
                                break
                        if resolved_local:
                            if kind == "to":
                                local_to.append(resolved_local)
                            elif kind == "cc":
                                local_cc.append(resolved_local)
                            else:
                                local_bcc.append(resolved_local)
                            continue

                    lookup_value = canonical.lower()
                    rows = None
                    if explicit_override and target_project_override is not None:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, Agent.id == AgentLink.b_agent_id)
                            .where(
                                AgentLink.a_project_id == project.id,
                                AgentLink.a_agent_id == sender.id,
                                AgentLink.status == "approved",
                                Project.id == target_project_override.id,
                                func.lower(Agent.name) == lookup_value,
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, Agent.id == AgentLink.b_agent_id)
                            .where(
                                AgentLink.a_project_id == project.id,
                                AgentLink.a_agent_id == sender.id,
                                AgentLink.status == "approved",
                                func.lower(Agent.name) == lookup_value,
                            )
                            .limit(1)
                        )

                    rec = rows.first() if rows else None
                    allow_auto_cross = not settings_local.contact_enforcement_enabled
                    if not rec and allow_auto_cross:
                        if explicit_override and target_project_override is not None:
                            try:
                                target_agent = await _get_or_create_agent(
                                    target_project_override,
                                    canonical,
                                    sender.program,
                                    sender.model,
                                    sender.task_description,
                                    settings_local,
                                )
                                ttl_seconds = int(getattr(settings_local, "contact_auto_ttl_seconds", 86400))
                                link = await _ensure_cross_project_link(
                                    project,
                                    sender,
                                    target_project_override,
                                    target_agent,
                                    ttl_seconds=ttl_seconds,
                                    reason="auto-cross-project",
                                )
                                rec = (link, target_project_override, target_agent)
                            except Exception:
                                rec = None
                        else:
                            try:
                                global_matches = [
                                    (proj, agent)
                                    for proj, agent in await _lookup_agents_any_project(canonical)
                                    if proj.id != project.id
                                ]
                            except Exception:
                                global_matches = []
                            if len(global_matches) == 1:
                                target_project_override, target_agent = global_matches[0]
                                try:
                                    ttl_seconds = int(getattr(settings_local, "contact_auto_ttl_seconds", 86400))
                                    link = await _ensure_cross_project_link(
                                        project,
                                        sender,
                                        target_project_override,
                                        target_agent,
                                        ttl_seconds=ttl_seconds,
                                        reason="auto-cross-project",
                                    )
                                    rec = (link, target_project_override, target_agent)
                                except Exception:
                                    rec = None
                            elif len(global_matches) > 1:
                                for proj, _agent in global_matches:
                                    label = proj.human_key or proj.slug or "(unknown project)"
                                    unknown_external[label].append(display_value or candidate.strip() or candidate)
                                continue
                    if rec:
                        _link, target_project, target_agent = rec
                        pol = (getattr(target_agent, "contact_policy", "auto") or "auto").lower()
                        if pol == "block_all":
                            await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                            raise _ContactBlocked()
                        bucket = external.setdefault(
                            target_project.id or 0,
                            {"project": target_project, "to": [], "cc": [], "bcc": []},
                        )
                        bucket[kind].append(target_agent.name)
                        continue

                    if explicit_override:
                        label = target_project_label or "(unknown project)"
                        unknown_external[label].append(display_value or candidate.strip() or candidate)
                    else:
                        unknown_local.add(display_value or candidate.strip() or candidate)

            try:
                await _route(to, "to")
                await _route(cc or [], "cc")
                await _route(bcc or [], "bcc")
            except _ContactBlocked as err:
                raise ToolExecutionError(
                    "CONTACT_BLOCKED",
                    "Recipient is not accepting messages.",
                    recoverable=True,
                ) from err

            if unknown_local or unknown_external:
                # Auto-register missing local recipients if enabled
                if getattr(settings_local, "messaging_auto_register_recipients", True):
                    # Best effort: try to register any unknown local recipients with sane defaults
                    newly_registered: set[str] = set()
                    for missing in list(unknown_local):
                        try:
                            _ = await _get_or_create_agent(
                                project,
                                missing,
                                sender.program,
                                sender.model,
                                sender.task_description,
                                settings_local,
                            )
                            newly_registered.add(missing)
                        except Exception:
                            pass
                    unknown_local.difference_update(newly_registered)
                    # Re-run routing for any that were registered
                    if newly_registered:
                        from contextlib import suppress
                        with suppress(_ContactBlocked):
                            await _route(list(newly_registered), "to")
                # Attempt cross-project handshakes for unknown external recipients if allowed
                attempted_external: list[str] = []
                try:
                    effective_auto_contact = bool(auto_contact_if_blocked or getattr(settings_local, "messaging_auto_handshake_on_block", True))
                    if effective_auto_contact and unknown_external:
                        from fastmcp.tools.tool import FunctionTool  # type: ignore
                        handshake = cast(FunctionTool, cast(Any, macro_contact_handshake))
                        # Iterate over a copy since we may mutate/resolve entries
                        for label, names in list(unknown_external.items()):
                            try:
                                target_proj = await _get_project_by_identifier(label)
                            except Exception:
                                continue
                            for nm in list(names):
                                try:
                                    await handshake.run(
                                        {
                                            "project_key": project.human_key,
                                            "requester": sender.name,
                                            "target": nm,
                                            "to_project": target_proj.human_key or target_proj.slug,
                                            "reason": "auto-handshake by send_message",
                                            "auto_accept": True,
                                            "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                            "register_if_missing": True,
                                        }
                                    )
                                    attempted_external.append(f"{nm}@{label}")
                                except Exception:
                                    pass
                        # Re-route any that we attempted to handshake for
                        if attempted_external:
                            from contextlib import suppress
                            with suppress(_ContactBlocked):
                                for item in attempted_external:
                                    await _route([item], "to")
                            # Purge unknown_external entries that now have approved links
                            try:
                                async with get_session() as scheck:
                                    for label, names in list(unknown_external.items()):
                                        try:
                                            tproj = await _get_project_by_identifier(label)
                                        except Exception:
                                            continue
                                        remaining: list[str] = []
                                        for nm in list(names):
                                            lookup_value = (nm or "").strip().lower()
                                            rows = await scheck.execute(
                                                select(AgentLink, Project, Agent)
                                                .join(Project, Project.id == AgentLink.b_project_id)
                                                .join(Agent, Agent.id == AgentLink.b_agent_id)
                                                .where(
                                                    AgentLink.a_project_id == project.id,
                                                    AgentLink.a_agent_id == sender.id,
                                                    AgentLink.status == "approved",
                                                    Project.id == tproj.id,
                                                    func.lower(Agent.name) == lookup_value,
                                                )
                                                .limit(1)
                                            )
                                            if rows.first() is None:
                                                remaining.append(nm)
                                        if remaining:
                                            unknown_external[label] = remaining
                                        else:
                                            unknown_external.pop(label, None)
                            except Exception:
                                pass
                except Exception:
                    pass
                # If everything resolved after auto-actions, skip error path
                still_unknown = bool(unknown_local) or any(v for v in unknown_external.values())
                if not still_unknown:
                    # All unknowns were resolved; continue to delivery
                    pass
                else:
                    parts: list[str] = []
                data_payload: dict[str, Any] = {}
                if still_unknown and unknown_local:
                    missing_local = sorted({name for name in unknown_local if name})
                    parts.append(
                        f"local recipients {', '.join(missing_local)} are not registered in project '{project.human_key}'"
                    )
                    data_payload["unknown_local"] = missing_local
                if still_unknown and unknown_external:
                    formatted_external = {
                        label: sorted({name for name in names if name})
                        for label, names in unknown_external.items()
                    }
                    ext_parts = [
                        f"{', '.join(names)} @ {label}"
                        for label, names in sorted(formatted_external.items())
                        if names
                    ]
                    if ext_parts:
                        parts.append(
                            "external recipients missing approved contact links: " + "; ".join(ext_parts)
                        )
                    data_payload["unknown_external"] = formatted_external
                # Include auto actions we tried
                if still_unknown and attempted_external:
                    data_payload["auto_contact_attempted_external"] = attempted_external
                if still_unknown:
                    hint = f"Use resource://agents/{project.slug} to list registered agents or register new identities."
                    parts.append(hint)
                    message = "Unable to send message — " + "; ".join(parts)
                    data_payload["hint"] = hint
                    # Provide concrete fix suggestions
                    try:
                        suggestions: list[dict[str, Any]] = []
                        for name in data_payload.get("unknown_local", [])[:5]:
                            suggestions.append(
                                {
                                    "tool": "register_agent",
                                    "arguments": {
                                        "project_key": project.human_key,
                                        "name": name,
                                        "program": sender.program,
                                        "model": sender.model,
                                        "task_description": sender.task_description,
                                    },
                                }
                            )
                        for label, names in (data_payload.get("unknown_external", {}) or {}).items():
                            for nm in names[:5]:
                                suggestions.append(
                                    {
                                        "tool": "macro_contact_handshake",
                                        "arguments": {
                                            "project_key": project.human_key,
                                            "requester": sender.name,
                                            "target": nm,
                                            "to_project": label,
                                            "auto_accept": True,
                                            "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                            "register_if_missing": True,
                                        },
                                    }
                                )
                        if suggestions:
                            data_payload["suggested_tool_calls"] = suggestions
                    except Exception:
                        pass
                    await ctx.error(f"RECIPIENT_NOT_FOUND: {message}")
                    raise ToolExecutionError(
                        "RECIPIENT_NOT_FOUND",
                        message,
                        recoverable=True,
                        data=data_payload,
                    )

        deliveries: list[dict[str, Any]] = []
        # Local deliver if any
        if local_to or local_cc or local_bcc:
            payload_local = await _deliver_message(
                ctx,
                "send_message",
                project,
                sender,
                local_to,
                local_cc,
                local_bcc,
                subject,
                body_md,
                attachment_paths,
                convert_images,
                importance,
                ack_required,
                thread_id,
            )
            deliveries.append({"project": project.human_key, "payload": payload_local})
        # External per-target project deliver (requires aliasing sender in target project)
        for _pid, group in external.items():
            p: Project = group["project"]
            try:
                alias = await _get_or_create_agent(p, sender.name, sender.program, sender.model, sender.task_description, settings_local)
                payload_ext = await _deliver_message(
                    ctx,
                    "send_message",
                    p,
                    alias,
                    group.get("to", []),
                    group.get("cc", []),
                    group.get("bcc", []),
                    subject,
                    body_md,
                    attachment_paths,
                    convert_images,
                    importance,
                    ack_required,
                    thread_id,
                )
                deliveries.append({"project": p.human_key, "payload": payload_ext})
            except Exception:
                continue

        # If a single delivery returned a structured error payload, bubble it up to top-level
        if len(deliveries) == 1:
            maybe_payload = deliveries[0].get("payload")
            if isinstance(maybe_payload, dict) and isinstance(maybe_payload.get("error"), dict):
                return {"error": maybe_payload["error"]}
        result: dict[str, Any] = {"deliveries": deliveries, "count": len(deliveries)}
        # Back-compat: expose top-level attachments when a single local delivery exists
        if len(deliveries) == 1:
            payload = deliveries[0].get("payload") or {}
            if isinstance(payload, dict) and "attachments" in payload:
                result["attachments"] = payload.get("attachments")
        return result

    @mcp.tool(name="reply_message")
    @_instrument_tool(
        "reply_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "write"},
        project_arg="project_key",
        agent_arg="sender_name",
    )
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

        Do / Don't
        ----------
        Do:
        - Keep the subject focused; avoid topic drift within a thread.
        - Reply to the original sender unless new stakeholders are strictly required.
        - Preserve importance/ack flags from the original unless there is a clear reason to change.
        - Use CC for FYI only; BCC sparingly and with intention.

        Don't:
        - Change `thread_id` when continuing the same discussion.
        - Escalate to many recipients; prefer targeted replies and start a new thread for new topics.
        - Attach large binaries in replies unless essential; reference prior attachments where possible.

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
        settings_local = get_settings()
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
        cc_list = cc or []
        bcc_list = bcc or []

        local_to: list[str] = []
        local_cc: list[str] = []
        local_bcc: list[str] = []
        external: dict[int, dict[str, Any]] = {}

        async with get_session() as sx:
            existing = await sx.execute(select(Agent.name).where(Agent.project_id == project.id))
            # Build case-insensitive lookup like send_message does
            local_lookup: dict[str, str] = {}
            for row in existing.fetchall():
                canonical_name = (row[0] or "").strip()
                if not canonical_name:
                    continue
                sanitized_canonical = sanitize_agent_name(canonical_name) or canonical_name
                for key in {canonical_name.lower(), sanitized_canonical.lower()}:
                    local_lookup.setdefault(key, canonical_name)
            unknown_local: set[str] = set()
            unknown_external: dict[str, list[str]] = defaultdict(list)

            class _ContactBlocked(Exception):
                pass

            async def _route(name_list: list[str], kind: str) -> None:
                for raw in name_list:
                    candidate = (raw or "").strip()
                    if not candidate:
                        continue
                    nm = candidate
                    explicit_override = False
                    target_project_override: Project | None = None
                    target_name_override: str | None = None
                    if candidate.startswith("project:") and "#" in candidate:
                        explicit_override = True
                        try:
                            _, rest = candidate.split(":", 1)
                            slug_part, agent_part = rest.split("#", 1)
                            target_project_override = await _get_project_by_identifier(slug_part.strip())
                            target_name_override = agent_part.strip()
                            nm = target_name_override or nm
                        except Exception:
                            unknown_external["(invalid project)"].append(candidate)
                            continue
                    elif "@" in candidate and not explicit_override:
                        name_part, project_part = candidate.split("@", 1)
                        if name_part.strip() and project_part.strip():
                            explicit_override = True
                            try:
                                target_project_override = await _get_project_by_identifier(project_part.strip())
                                target_name_override = name_part.strip()
                                nm = target_name_override
                            except Exception:
                                label = project_part.strip() or "(invalid project)"
                                unknown_external[label].append(candidate)
                                continue
                    # Use case-insensitive lookup like send_message
                    if not explicit_override:
                        sanitized_nm = sanitize_agent_name(nm) or nm
                        key_candidates = {nm.lower(), sanitized_nm.lower()}
                        resolved_local = None
                        for key in key_candidates:
                            resolved_local = local_lookup.get(key)
                            if resolved_local:
                                break
                        if resolved_local:
                            if kind == "to":
                                local_to.append(resolved_local)
                            elif kind == "cc":
                                local_cc.append(resolved_local)
                            else:
                                local_bcc.append(resolved_local)
                            continue
                    rows = None
                    if target_project_override is not None and target_name_override:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, Agent.id == AgentLink.b_agent_id)
                            .where(
                                AgentLink.a_project_id == project.id,
                                AgentLink.a_agent_id == sender.id,
                                AgentLink.status == "approved",
                                Project.id == target_project_override.id,
                                Agent.name == target_name_override,
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, Agent.id == AgentLink.b_agent_id)
                            .where(
                                AgentLink.a_project_id == project.id,
                                AgentLink.a_agent_id == sender.id,
                                AgentLink.status == "approved",
                                Agent.name == nm,
                            )
                            .limit(1)
                        )
                    rec = rows.first()
                    allow_auto_cross = not settings_local.contact_enforcement_enabled
                    if not rec and allow_auto_cross:
                        if explicit_override and target_project_override is not None:
                            try:
                                target_agent = await _get_or_create_agent(
                                    target_project_override,
                                    target_name_override,
                                    sender.program,
                                    sender.model,
                                    sender.task_description,
                                    settings_local,
                                )
                                ttl_seconds = int(getattr(settings_local, "contact_auto_ttl_seconds", 86400))
                                link = await _ensure_cross_project_link(
                                    project,
                                    sender,
                                    target_project_override,
                                    target_agent,
                                    ttl_seconds=ttl_seconds,
                                    reason="auto-cross-project",
                                )
                                rec = (link, target_project_override, target_agent)
                            except Exception:
                                rec = None
                        else:
                            try:
                                global_matches = [
                                    (proj, agent)
                                    for proj, agent in await _lookup_agents_any_project(nm)
                                    if proj.id != project.id
                                ]
                            except Exception:
                                global_matches = []
                            if len(global_matches) == 1:
                                target_project_override, target_agent = global_matches[0]
                                try:
                                    ttl_seconds = int(getattr(settings_local, "contact_auto_ttl_seconds", 86400))
                                    link = await _ensure_cross_project_link(
                                        project,
                                        sender,
                                        target_project_override,
                                        target_agent,
                                        ttl_seconds=ttl_seconds,
                                        reason="auto-cross-project",
                                    )
                                    rec = (link, target_project_override, target_agent)
                                except Exception:
                                    rec = None
                            elif len(global_matches) > 1:
                                for proj, _agent in global_matches:
                                    label = proj.human_key or proj.slug or "(unknown project)"
                                    unknown_external.setdefault(label, []).append(nm)
                                continue
                    if rec:
                        _link, target_project, target_agent = rec
                        recipient_policy = (getattr(target_agent, "contact_policy", "auto") or "auto").lower()
                        if recipient_policy == "block_all":
                            await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                            raise _ContactBlocked()
                        bucket = external.setdefault(target_project.id or 0, {"project": target_project, "to": [], "cc": [], "bcc": []})
                        bucket[kind].append(target_agent.name)
                    else:
                        # No link found - add to unknown recipients for later validation
                        if explicit_override:
                            label = (
                                target_project_override.human_key or target_project_override.slug
                                if target_project_override
                                else "(unknown project)"
                            )
                            unknown_external[label].append(candidate)
                        else:
                            unknown_local.add(candidate)

        try:
            await _route(to_names, "to")
            await _route(cc_list, "cc")
            await _route(bcc_list, "bcc")
        except _ContactBlocked:
            return {"error": {"type": "CONTACT_BLOCKED", "message": "Recipient is not accepting messages."}}

        # Validate all recipients were resolved
        if unknown_local or unknown_external:
            parts: list[str] = []
            data_payload: dict[str, Any] = {}
            if unknown_local:
                missing_local = sorted({name for name in unknown_local if name})
                parts.append(
                    f"local recipients {', '.join(missing_local)} are not registered in project '{project.human_key}'"
                )
                data_payload["unknown_local"] = missing_local
            if unknown_external:
                formatted_external = {
                    label: sorted({name for name in names if name})
                    for label, names in unknown_external.items()
                }
                ext_parts = [
                    f"{', '.join(names)} @ {label}"
                    for label, names in sorted(formatted_external.items())
                    if names
                ]
                if ext_parts:
                    parts.append(
                        "external recipients missing approved contact links: " + "; ".join(ext_parts)
                    )
                data_payload["unknown_external"] = formatted_external
            hint = f"Use resource://agents/{project.slug} to list registered agents or register new identities."
            parts.append(hint)
            message = "Unable to send reply — " + "; ".join(parts)
            raise ToolExecutionError(
                "RECIPIENT_UNKNOWN",
                message,
                recoverable=True,
                data=data_payload,
            )

        deliveries: list[dict[str, Any]] = []
        if local_to or local_cc or local_bcc:
            payload_local = await _deliver_message(
                ctx,
                "reply_message",
                project,
                sender,
                local_to,
                local_cc,
                local_bcc,
                reply_subject,
                body_md,
                None,
                None,
                importance=original.importance,
                ack_required=original.ack_required,
                thread_id=thread_key,
            )
            deliveries.append({"project": project.human_key, "payload": payload_local})

        for _pid, group in external.items():
            target_project: Project = group["project"]
            try:
                alias = await _get_or_create_agent(
                    target_project,
                    sender.name,
                    sender.program,
                    sender.model,
                    sender.task_description,
                    settings_local,
                )
                payload_ext = await _deliver_message(
                    ctx,
                    "reply_message",
                    target_project,
                    alias,
                    group.get("to", []),
                    group.get("cc", []),
                    group.get("bcc", []),
                    reply_subject,
                    body_md,
                    None,
                    None,
                    importance=original.importance,
                    ack_required=original.ack_required,
                    thread_id=thread_key,
                )
                deliveries.append({"project": target_project.human_key, "payload": payload_ext})
            except Exception:
                continue

        if not deliveries:
            return {
                "thread_id": thread_key,
                "reply_to": message_id,
                "deliveries": [],
                "count": 0,
            }

        base_payload = deliveries[0].get("payload") or {}
        primary_payload = dict(base_payload) if isinstance(base_payload, dict) else {}
        primary_payload.setdefault("thread_id", thread_key)
        primary_payload["reply_to"] = message_id
        primary_payload["deliveries"] = deliveries
        primary_payload["count"] = len(deliveries)
        if len(deliveries) == 1:
            attachments = base_payload.get("attachments") if isinstance(base_payload, dict) else None
            if attachments is not None:
                primary_payload.setdefault("attachments", attachments)
        return primary_payload

    @mcp.tool(name="request_contact")
    @_instrument_tool(
        "request_contact",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact"},
        project_arg="project_key",
        agent_arg="from_agent",
    )
    async def request_contact(
        ctx: Context,
        project_key: str,
        from_agent: str,
        to_agent: str,
        to_project: Optional[str] = None,
        reason: str = "",
        ttl_seconds: int = 7 * 24 * 3600,
        # Optional quality-of-life flags; ignored by clients that don't pass them
        register_if_missing: bool = True,
        program: Optional[str] = None,
        model: Optional[str] = None,
        task_description: Optional[str] = None,
    ) -> dict[str, Any]:
        """Request contact approval to message another agent.

        Creates (or refreshes) a pending AgentLink and sends a small ack_required intro message.

        Discovery
        ---------
        To discover available agent names, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        from_agent : str
            Your agent name (must be registered in the project).
        to_agent : str
            Target agent name (use resource://agents/{project_key} to discover names).
        to_project : Optional[str]
            Target project if different from your project (cross-project coordination).
        reason : str
            Optional explanation for the contact request.
        ttl_seconds : int
            Time to live for the contact approval request (default: 7 days).
        """
        project = await _get_project_by_identifier(project_key)
        settings = get_settings()
        a = await _get_agent(project, from_agent)
        # Allow explicit external addressing in to_agent as project:<slug>#<Name>
        target_project = project
        target_name = to_agent
        if to_project:
            target_project = await _get_project_by_identifier(to_project)
        elif to_agent.startswith("project:") and "#" in to_agent:
            try:
                _, rest = to_agent.split(":", 1)
                slug_part, agent_part = rest.split("#", 1)
                target_project = await _get_project_by_identifier(slug_part)
                target_name = agent_part.strip()
            except Exception:
                target_project = project
                target_name = to_agent
        try:
            b = await _get_agent(target_project, target_name)
        except NoResultFound:
            cleaned_target = sanitize_agent_name(target_name or "")
            if register_if_missing and cleaned_target:
                b = await _get_or_create_agent(
                    target_project,
                    target_name,
                    program or "unknown",
                    model or "unknown",
                    task_description or "",
                    settings,
                )
            else:
                raise
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=max(60, ttl_seconds))
        async with get_session() as s:
            # upsert link
            existing = await s.execute(
                select(AgentLink).where(
                    AgentLink.a_project_id == project.id,
                    AgentLink.a_agent_id == a.id,
                    AgentLink.b_project_id == target_project.id,
                    AgentLink.b_agent_id == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                link.status = "pending"
                link.reason = reason
                link.updated_ts = now
                link.expires_ts = exp
                s.add(link)
            else:
                link = AgentLink(
                    a_project_id=project.id or 0,
                    a_agent_id=a.id or 0,
                    b_project_id=target_project.id or 0,
                    b_agent_id=b.id or 0,
                    status="pending",
                    reason=reason,
                    created_ts=now,
                    updated_ts=now,
                    expires_ts=exp,
                )
                s.add(link)
            await s.commit()
        # Send an intro message with ack_required
        subject = f"Contact request from {a.name}"
        body = reason or f"{a.name} requests permission to contact {b.name}."
        await _deliver_message(
            ctx,
            "request_contact",
            target_project,
            a,
            [b.name],
            [],
            [],
            subject,
            body,
            None,
            None,
            importance="normal",
            ack_required=True,
            thread_id=None,
        )
        return {"from": a.name, "from_project": project.human_key, "to": b.name, "to_project": target_project.human_key, "status": "pending", "expires_ts": _iso(exp)}

    @mcp.tool(name="respond_contact")
    @_instrument_tool(
        "respond_contact",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact"},
        project_arg="project_key",
        agent_arg="to_agent",
    )
    async def respond_contact(
        ctx: Context,
        project_key: str,
        to_agent: str,
        from_agent: str,
        accept: bool,
        ttl_seconds: int = 30 * 24 * 3600,
        from_project: Optional[str] = None,
    ) -> dict[str, Any]:
        """Approve or deny a contact request."""
        project = await _get_project_by_identifier(project_key)
        # Resolve remote requestor project if provided
        a_project = project if not from_project else await _get_project_by_identifier(from_project)
        a = await _get_agent(a_project, from_agent)
        b = await _get_agent(project, to_agent)
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=max(60, ttl_seconds)) if accept else None
        updated = 0
        async with get_session() as s:
            existing = await s.execute(
                select(AgentLink).where(
                    AgentLink.a_project_id == a_project.id,
                    AgentLink.a_agent_id == a.id,
                    AgentLink.b_project_id == project.id,
                    AgentLink.b_agent_id == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                link.status = "approved" if accept else "blocked"
                link.updated_ts = now
                link.expires_ts = exp
                s.add(link)
                updated = 1
            else:
                if accept:
                    s.add(AgentLink(
                        a_project_id=project.id or 0,
                        a_agent_id=a.id or 0,
                        b_project_id=project.id or 0,
                        b_agent_id=b.id or 0,
                        status="approved",
                        reason="",
                        created_ts=now,
                        updated_ts=now,
                        expires_ts=exp,
                    ))
                    updated = 1
            await s.commit()
        await ctx.info(f"Contact {'approved' if accept else 'denied'}: {from_agent} -> {to_agent}")
        return {"from": from_agent, "to": to_agent, "approved": bool(accept), "expires_ts": _iso(exp) if exp else None, "updated": updated}

    @mcp.tool(name="list_contacts")
    @_instrument_tool(
        "list_contacts",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact", "audit"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def list_contacts(ctx: Context, project_key: str, agent_name: str) -> list[dict[str, Any]]:
        """List contact links for an agent in a project."""
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        out: list[dict[str, Any]] = []
        async with get_session() as s:
            rows = await s.execute(
                select(AgentLink, Agent.name)
                .join(Agent, Agent.id == AgentLink.b_agent_id)
                .where(AgentLink.a_project_id == project.id, AgentLink.a_agent_id == agent.id)
            )
            for link, name in rows.all():
                out.append({
                    "to": name,
                    "status": link.status,
                    "reason": link.reason,
                    "updated_ts": _iso(link.updated_ts),
                    "expires_ts": _iso(link.expires_ts) if link.expires_ts else None,
                })
        return out

    @mcp.tool(name="set_contact_policy")
    @_instrument_tool(
        "set_contact_policy",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact", "configure"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def set_contact_policy(ctx: Context, project_key: str, agent_name: str, policy: str) -> dict[str, Any]:
        """Set contact policy for an agent: open | auto | contacts_only | block_all."""
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        pol = (policy or "auto").lower()
        if pol not in {"open", "auto", "contacts_only", "block_all"}:
            pol = "auto"
        async with get_session() as s:
            db_agent = await s.get(Agent, agent.id)
            if db_agent:
                db_agent.contact_policy = pol
                s.add(db_agent)
                await s.commit()
        return {"agent": agent.name, "policy": pol}

    @mcp.tool(name="fetch_inbox")
    @_instrument_tool(
        "fetch_inbox",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
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
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nlimit={limit}\nurgent_only={urgent_only}", title="tool: fetch_inbox", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _get_agent(project, agent_name)
            items = await _list_inbox(project, agent, limit, urgent_only, include_bodies, since_ts)
            await ctx.info(f"Fetched {len(items)} messages for '{agent.name}'. urgent_only={urgent_only}")
            return items
        except Exception as exc:
            _rich_error_panel("fetch_inbox", {"error": str(exc)})
            raise

    @mcp.tool(name="mark_message_read")
    @_instrument_tool(
        "mark_message_read",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
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

        Idempotency
        -----------
        - If `mark_message_read` has already been called earlier for the same (agent, message),
          the original timestamp is returned and no error is raised.

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
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nmessage_id={message_id}", title="tool: mark_message_read", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _get_agent(project, agent_name)
            await _get_message(project, message_id)
            read_ts = await _update_recipient_timestamp(agent, message_id, "read_ts")
            await ctx.info(f"Marked message {message_id} read for '{agent.name}'.")
            return {"message_id": message_id, "read": bool(read_ts), "read_at": _iso(read_ts) if read_ts else None}
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    from rich.console import Console  # type: ignore
                    from rich.json import JSON  # type: ignore

                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="acknowledge_message")
    @_instrument_tool(
        "acknowledge_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "ack"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
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

        Idempotency
        -----------
        - If acknowledgement already exists, the previous timestamps are preserved and returned.

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
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nmessage_id={message_id}", title="tool: acknowledge_message", border_style="green"))
            except Exception:
                pass
        try:
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
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    import importlib as _imp
                    _rc = _imp.import_module("rich.console")
                    _rj = _imp.import_module("rich.json")
                    Console = _rc.Console
                    JSON = _rj.JSON
                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="macro_start_session")
    @_instrument_tool(
        "macro_start_session",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "messaging", "file_reservations", "identity"},
        project_arg="human_key",
        agent_arg="agent_name",
    )
    async def macro_start_session(
        ctx: Context,
        human_key: str,
        program: str,
        model: str,
        task_description: str = "",
        agent_name: Optional[str] = None,
        file_reservation_paths: Optional[list[str]] = None,
        file_reservation_reason: str = "macro-session",
        file_reservation_ttl_seconds: int = 3600,
        inbox_limit: int = 10,
    ) -> dict[str, Any]:
        """
        Macro helper that boots a project session: ensure project, register agent,
        optionally file_reservation paths, and fetch the latest inbox snapshot.
        """
        settings = get_settings()
        project = await _ensure_project(human_key)
        agent = await _get_or_create_agent(project, agent_name, program, model, task_description, settings)

        file_reservations_result: Optional[dict[str, Any]] = None
        if file_reservation_paths:
            # Use MCP tool registry to avoid param shadowing (file_reservation_paths param shadows file_reservation_paths function)
            from fastmcp.tools.tool import FunctionTool
            _file_reservation_tool = cast(FunctionTool, await mcp.get_tool("file_reservation_paths"))
            _file_reservation_run = await _file_reservation_tool.run({
                "project_key": project.human_key,
                "agent_name": agent.name,
                "paths": file_reservation_paths,
                "ttl_seconds": file_reservation_ttl_seconds,
                "exclusive": True,
                "reason": file_reservation_reason,
            })
            file_reservations_result = cast(dict[str, Any], _file_reservation_run.structured_content or {})

        inbox_items = await _list_inbox(
            project,
            agent,
            inbox_limit,
            urgent_only=False,
            include_bodies=False,
            since_ts=None,
        )
        await ctx.info(
            f"macro_start_session prepared agent '{agent.name}' on project '{project.human_key}' "
            f"(file_reservations={len(file_reservations_result['granted']) if file_reservations_result else 0})."
        )
        return {
            "project": _project_to_dict(project),
            "agent": _agent_to_dict(agent),
            "file_reservations": file_reservations_result or {"granted": [], "conflicts": []},
            "inbox": inbox_items,
        }

    @mcp.tool(name="macro_prepare_thread")
    @_instrument_tool(
        "macro_prepare_thread",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "messaging", "summarization"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def macro_prepare_thread(
        ctx: Context,
        project_key: str,
        thread_id: str,
        program: str,
        model: str,
        agent_name: Optional[str] = None,
        task_description: str = "",
        register_if_missing: bool = True,
        include_examples: bool = True,
        inbox_limit: int = 10,
        include_inbox_bodies: bool = False,
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Macro helper that aligns an agent with an existing thread by ensuring registration,
        summarising the thread, and fetching recent inbox context.
        """
        settings = get_settings()
        project = await _get_project_by_identifier(project_key)
        if register_if_missing:
            agent = await _get_or_create_agent(project, agent_name, program, model, task_description, settings)
        else:
            if not agent_name:
                raise ValueError("agent_name is required when register_if_missing is False.")
            agent = await _get_agent(project, agent_name)

        inbox_items = await _list_inbox(
            project,
            agent,
            inbox_limit,
            urgent_only=False,
            include_bodies=include_inbox_bodies,
            since_ts=None,
        )
        summary, examples, total_messages = await _compute_thread_summary(
            project,
            thread_id,
            include_examples,
            llm_mode,
            llm_model,
        )
        await ctx.info(
            f"macro_prepare_thread prepared agent '{agent.name}' for thread '{thread_id}' "
            f"on project '{project.human_key}' (messages={total_messages})."
        )
        return {
            "project": _project_to_dict(project),
            "agent": _agent_to_dict(agent),
            "thread": {"thread_id": thread_id, "summary": summary, "examples": examples, "total_messages": total_messages},
            "inbox": inbox_items,
        }

    @mcp.tool(name="macro_file_reservation_cycle")
    @_instrument_tool(
        "macro_file_reservation_cycle",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "file_reservations", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def macro_file_reservation_cycle(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "macro-file_reservation",
        auto_release: bool = False,
    ) -> dict[str, Any]:
        """Reserve a set of file paths and optionally release them at the end of the workflow."""

        # Call underlying FunctionTool directly so we don't treat the wrapper as a plain coroutine
        from fastmcp.tools.tool import FunctionTool
        file_reservations_tool = cast(FunctionTool, cast(Any, file_reservation_paths))
        file_reservations_tool_result = await file_reservations_tool.run({
            "project_key": project_key,
            "agent_name": agent_name,
            "paths": paths,
            "ttl_seconds": ttl_seconds,
            "exclusive": exclusive,
            "reason": reason,
        })
        file_reservations_result = cast(dict[str, Any], file_reservations_tool_result.structured_content or {})

        release_result = None
        if auto_release:
            release_tool = cast(FunctionTool, cast(Any, release_file_reservations_tool))
            release_tool_result = await release_tool.run({
                "project_key": project_key,
                "agent_name": agent_name,
                "paths": paths,
            })
            release_result = cast(dict[str, Any], release_tool_result.structured_content or {})

        await ctx.info(
            f"macro_file_reservation_cycle issued {len(file_reservations_result['granted'])} file_reservation(s) for '{agent_name}' on '{project_key}'" +
            (" and released them immediately." if auto_release else ".")
        )
        return {
            "file_reservations": file_reservations_result,
            "released": release_result,
        }

    @mcp.tool(name="macro_contact_handshake")
    @_instrument_tool(
        "macro_contact_handshake",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "contact", "messaging"},
        project_arg="project_key",
        agent_arg="requester",
    )
    async def macro_contact_handshake(
        ctx: Context,
        project_key: str,
        requester: Optional[str] = None,
        target: Optional[str] = None,
        reason: str = "",
        ttl_seconds: int = 7 * 24 * 3600,
        auto_accept: bool = False,
        welcome_subject: Optional[str] = None,
        welcome_body: Optional[str] = None,
        to_project: Optional[str] = None,
        # Aliases for compatibility
        agent_name: Optional[str] = None,
        to_agent: Optional[str] = None,
        register_if_missing: bool = True,
        program: Optional[str] = None,
        model: Optional[str] = None,
        task_description: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Request contact permissions and optionally auto-approve plus send a welcome message."""

        # Resolve aliases
        real_requester = (requester or agent_name or "").strip()
        real_target = (target or to_agent or "").strip()
        target_project_key = (to_project or "").strip()
        if not real_requester or not real_target:
            # Best-effort inference to honor "obvious intent"
            try:
                project = await _get_project_by_identifier(project_key)
                # If requester missing and exactly one agent exists in project, assume that one
                if not real_requester and project.id is not None:
                    async with get_session() as s:
                        rows = await s.execute(select(Agent.name).where(Agent.project_id == project.id))
                        names = [str(row[0]).strip() for row in rows.fetchall() if (row and row[0])]
                    if len(names) == 1:
                        real_requester = names[0]
                # If target missing and exactly two agents exist, infer the other
                if not real_target and project.id is not None:
                    async with get_session() as s2:
                        rows2 = await s2.execute(select(Agent.name).where(Agent.project_id == project.id))
                        names2 = [str(row[0]).strip() for row in rows2.fetchall() if (row and row[0])]
                    if real_requester and len(names2) == 2 and real_requester in names2:
                        real_target = next((n for n in names2 if n != real_requester), real_target)
            except Exception:
                pass
        if not real_requester or not real_target:
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "macro_contact_handshake requires requester/agent_name and target/to_agent",
                recoverable=True,
                data={
                    "requester": real_requester or requester,
                    "agent_name": agent_name,
                    "target": real_target or target,
                    "to_agent": to_agent,
                    "suggested_tool_calls": [
                        {
                            "tool": "macro_contact_handshake",
                            "arguments": {
                                "project_key": project_key,
                                "requester": real_requester or "<your_agent>",
                                "target": real_target or "<their_agent>",
                                "auto_accept": True,
                                "ttl_seconds": ttl_seconds,
                            },
                        }
                    ],
                },
            )

        from fastmcp.tools.tool import FunctionTool
        request_tool = cast(FunctionTool, cast(Any, request_contact))
        request_payload: dict[str, Any] = {
            "project_key": project_key,
            "from_agent": real_requester,
            "to_agent": real_target,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
        }
        if target_project_key:
            request_payload["to_project"] = target_project_key
        if register_if_missing:
            request_payload["register_if_missing"] = True
        if program:
            request_payload["program"] = program
        if model:
            request_payload["model"] = model
        if task_description:
            request_payload["task_description"] = task_description
        request_tool_result = await request_tool.run(request_payload)
        request_result = cast(dict[str, Any], request_tool_result.structured_content or {})

        response_result = None
        if auto_accept:
            respond_tool = cast(FunctionTool, cast(Any, respond_contact))
            respond_payload: dict[str, Any] = {
                "project_key": target_project_key or project_key,
                "to_agent": real_target,
                "from_agent": real_requester,
                "accept": True,
                "ttl_seconds": ttl_seconds,
            }
            if target_project_key:
                respond_payload["from_project"] = project_key
            respond_tool_result = await respond_tool.run(respond_payload)
            response_result = cast(dict[str, Any], respond_tool_result.structured_content or {})

        welcome_result = None
        if welcome_subject and welcome_body and not target_project_key:
            try:
                send_tool = cast(FunctionTool, cast(Any, send_message))
                send_tool_result = await send_tool.run({
                    "project_key": project_key,
                    "sender_name": real_requester,
                    "to": [real_target],
                    "subject": welcome_subject,
                    "body_md": welcome_body,
                    "thread_id": thread_id,
                })
                welcome_result = cast(dict[str, Any], send_tool_result.structured_content or {})
            except ToolExecutionError as exc:
                # surface but do not abort handshake
                await ctx.debug(f"macro_contact_handshake failed to send welcome: {exc}")

        return {
            "request": request_result,
            "response": response_result,
            "welcome_message": welcome_result,
        }

    @mcp.tool(name="search_messages")
    @_instrument_tool("search_messages", cluster=CLUSTER_SEARCH, capabilities={"search"}, project_arg="project_key")
    async def search_messages(
        ctx: Context,
        project_key: str,
        query: str,
        limit: int = 20,
    ) -> Any:
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
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                _rt = _imp.import_module("rich.text")
                Console = _rc.Console
                Panel = _rp.Panel
                Text = _rt.Text
                cons = Console()
                body = Text.assemble(
                    ("project: ", "cyan"), (project.human_key, "white"), "\n",
                    ("query: ", "cyan"), (query[:200], "white"), "\n",
                    ("limit: ", "cyan"), (str(limit), "white"),
                )
                cons.print(Panel(body, title="tool: search_messages", border_style="green"))
            except Exception:
                pass
        if project.id is None:
            raise ValueError("Project must have an id before searching messages.")
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                           m.thread_id, a.name AS sender_name
                    FROM fts_messages
                    JOIN messages m ON fts_messages.rowid = m.id
                    JOIN agents a ON m.sender_id = a.id
                    WHERE m.project_id = :project_id AND fts_messages MATCH :query
                    ORDER BY bm25(fts_messages) ASC
                    LIMIT :limit
                    """
                ),
                {"project_id": project.id, "query": query, "limit": limit},
            )
            rows = result.mappings().all()
        await ctx.info(f"Search '{query}' returned {len(rows)} messages for project '{project.human_key}'.")
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel(f"results={len(rows)}", title="tool: search_messages — done", border_style="green"))
            except Exception:
                pass
        items = [
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
        try:
            from fastmcp.tools.tool import ToolResult  # type: ignore
            return ToolResult(structured_content={"result": items})
        except Exception:
            return items

    @mcp.tool(name="summarize_thread")
    @_instrument_tool("summarize_thread", cluster=CLUSTER_SEARCH, capabilities={"summarization", "search"}, project_arg="project_key")
    async def summarize_thread(
        ctx: Context,
        project_key: str,
        thread_id: str,
        include_examples: bool = False,
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
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
        summary, examples, total_messages = await _compute_thread_summary(
            project,
            thread_id,
            include_examples,
            llm_mode,
            llm_model,
                )
        await ctx.info(
            f"Summarized thread '{thread_id}' for project '{project.human_key}' with {total_messages} messages"
        )
        return {"thread_id": thread_id, "summary": summary, "examples": examples}

    @mcp.tool(name="summarize_threads")
    @_instrument_tool("summarize_threads", cluster=CLUSTER_SEARCH, capabilities={"summarization", "search"}, project_arg="project_key")
    async def summarize_threads(
        ctx: Context,
        project_key: str,
        thread_ids: list[str],
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
        per_thread_limit: int = 50,
    ) -> dict[str, Any]:
        """
        Produce a digest across multiple threads including top mentions and action items.

        Parameters
        ----------
        project_key : str
            Project identifier.
        thread_ids : list[str]
            Collection of thread keys or seed message ids.
        llm_mode : bool
            If true and LLM is enabled, refine the digest with the LLM for clarity.
        llm_model : Optional[str]
            Override model name for the LLM call.
        per_thread_limit : int
            Max messages to consider per thread.

        Returns
        -------
        dict
            {
              threads: [{thread_id, summary}],
              aggregate: { top_mentions[], action_items[], key_points[] }
            }
        """
        project = await _get_project_by_identifier(project_key)
        if project.id is None:
            raise ValueError("Project must have an id before summarizing threads.")
        await ensure_schema()

        sender_alias = aliased(Agent)
        all_mentions: dict[str, int] = {}
        all_actions: list[str] = []
        all_points: list[str] = []
        thread_summaries: list[dict[str, Any]] = []

        async with get_session() as session:
            for tid in thread_ids:
                try:
                    seed_id = int(tid)
                except ValueError:
                    seed_id = None
                criteria = [Message.thread_id == tid]
                if seed_id is not None:
                    criteria.append(Message.id == seed_id)
                stmt = (
                    select(Message, sender_alias.name)
                    .join(sender_alias, Message.sender_id == sender_alias.id)
                    .where(Message.project_id == project.id, or_(*criteria))
                    .order_by(asc(Message.created_ts))
                    .limit(per_thread_limit)
                )
                rows = (await session.execute(stmt)).all()
                summary = _summarize_messages(rows)
                # accumulate
                for m in summary.get("mentions", []):
                    name = str(m.get("name", "")).strip()
                    if not name:
                        continue
                    all_mentions[name] = all_mentions.get(name, 0) + int(m.get("count", 0) or 0)
                all_actions.extend(summary.get("action_items", []))
                all_points.extend(summary.get("key_points", []))
                thread_summaries.append({"thread_id": tid, "summary": summary})

        # Lightweight heuristic digest
        top_mentions = sorted(all_mentions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        aggregate = {
            "top_mentions": [{"name": n, "count": c} for n, c in top_mentions],
            "action_items": all_actions[:25],
            "key_points": all_points[:25],
        }

        # Optional LLM refinement
        if llm_mode and get_settings().llm.enabled and thread_summaries:
            try:
                # Compose compact context combining per-thread key points & actions only
                parts: list[str] = []
                for item in thread_summaries[:8]:
                    s = item["summary"]
                    parts.append(
                        "\n".join(
                            [
                                f"# Thread {item['thread_id']}",
                                "## Key Points",
                                *[f"- {p}" for p in s.get("key_points", [])[:6]],
                                "## Actions",
                                *[f"- {a}" for a in s.get("action_items", [])[:6]],
                            ]
                        )
                    )
                system = (
                    "You are a senior engineer producing a crisp digest across threads. "
                    "Return JSON: { threads: [{thread_id, key_points[], actions[]}], aggregate: {top_mentions[], key_points[], action_items[]} }."
                )
                user = "\n\n".join(parts)
                llm_resp = await complete_system_user(system, user, model=llm_model)
                parsed = _parse_json_safely(llm_resp.content)
                if parsed:
                    agg = parsed.get("aggregate") or {}
                    if agg:
                        for k in ("top_mentions", "key_points", "action_items"):
                            v = agg.get(k)
                            if v:
                                aggregate[k] = v
                    # Replace per-thread summaries' key aggregates if returned
                    revised_threads = []
                    threads_payload = parsed.get("threads") or []
                    if threads_payload:
                        mapping = {str(t.get("thread_id")): t for t in threads_payload}
                        for item in thread_summaries:
                            tid = str(item["thread_id"])
                            if tid in mapping:
                                s = item["summary"].copy()
                                tdata = mapping[tid]
                                if tdata.get("key_points"):
                                    s["key_points"] = tdata["key_points"]
                                if tdata.get("actions"):
                                    s["action_items"] = tdata["actions"]
                                revised_threads.append({"thread_id": item["thread_id"], "summary": s})
                            else:
                                revised_threads.append(item)
                        thread_summaries = revised_threads
            except Exception as e:
                await ctx.debug(f"summarize_threads.llm_skipped: {e}")

        await ctx.info(f"Summarized {len(thread_ids)} thread(s) for project '{project.human_key}'.")
        return {"threads": thread_summaries, "aggregate": aggregate}

    @mcp.tool(name="install_precommit_guard")
    @_instrument_tool("install_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"}, project_arg="project_key")
    async def install_precommit_guard(
        ctx: Context,
        project_key: str,
        code_repo_path: str,
    ) -> dict[str, Any]:
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nrepo={code_repo_path}", title="tool: install_precommit_guard", border_style="green"))
            except Exception:
                pass
        project = await _get_project_by_identifier(project_key)
        repo_path = Path(code_repo_path).expanduser().resolve()
        hook_path = await install_guard_script(settings, project.slug, repo_path)
        await ctx.info(f"Installed pre-commit guard for project '{project.human_key}' at {hook_path}.")
        return {"hook": str(hook_path)}

    @mcp.tool(name="uninstall_precommit_guard")
    @_instrument_tool("uninstall_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"})
    async def uninstall_precommit_guard(
        ctx: Context,
        code_repo_path: str,
    ) -> dict[str, Any]:
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"repo={code_repo_path}", title="tool: uninstall_precommit_guard", border_style="green"))
            except Exception:
                pass
        repo_path = Path(code_repo_path).expanduser().resolve()
        removed = await uninstall_guard_script(repo_path)
        if removed:
            await ctx.info(f"Removed pre-commit guard at {repo_path / '.git/hooks/pre-commit'}.")
        else:
            await ctx.info(f"No pre-commit guard to remove at {repo_path / '.git/hooks/pre-commit'}.")
        return {"removed": removed}

    @mcp.tool(name="file_reservation_paths")
    @_instrument_tool("file_reservation_paths", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations", "repository"}, project_arg="project_key", agent_arg="agent_name")
    async def file_reservation_paths(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """
        Request advisory file reservations (leases) on project-relative paths/globs.

        Semantics
        ---------
        - Conflicts are reported if an overlapping active exclusive reservation exists held by another agent
        - Glob matching is symmetric (`fnmatchcase(a,b)` or `fnmatchcase(b,a)`), including exact matches
        - When granted, a JSON artifact is written under `file_reservations/<sha1(path)>.json` and the DB is updated
        - TTL must be >= 60 seconds (enforced by the server settings/policy)

        Do / Don't
        ----------
        Do:
        - Reserve files before starting edits to signal intent to other agents.
        - Use specific, minimal patterns (e.g., `app/api/*.py`) instead of broad globs.
        - Set a realistic TTL and renew with `renew_file_reservations` if you need more time.

        Don't:
        - Reserve the entire repository or very broad patterns (e.g., `**/*`) unless absolutely necessary.
        - Hold long-lived exclusive reservations when you are not actively editing.
        - Ignore conflicts; resolve them by coordinating with holders or waiting for expiry.

        Parameters
        ----------
        project_key : str
        agent_name : str
        paths : list[str]
            File paths or glob patterns relative to the project workspace (e.g., "app/api/*.py").
        ttl_seconds : int
            Time to live for the file_reservation; expired file_reservations are auto-released.
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
        {"jsonrpc":"2.0","id":"12","method":"tools/call","params":{"name":"file_reservation_paths","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle","paths":["app/api/*.py"],
          "ttl_seconds":7200,"exclusive":true,"reason":"migrations"
        }}}
        ```
        """
        project = await _get_project_by_identifier(project_key)
        settings = get_settings()
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                c = Console()
                c.print(Panel("\n".join(paths), title=f"tool: file_reservation_paths — agent={agent_name} ttl={ttl_seconds}s", border_style="green"))
            except Exception:
                pass
        agent = await _get_agent(project, agent_name)
        if project.id is None:
            raise ValueError("Project must have an id before reserving file paths.")
        stale_auto_releases = await _expire_stale_file_reservations(project.id)
        if stale_auto_releases:
            summary = ", ".join(
                f"{status.agent.name}:{status.reservation.path_pattern}"
                for status in stale_auto_releases[:5]
            )
            extra = f" ({summary})" if summary else ""
            await ctx.info(f"Auto-released {len(stale_auto_releases)} stale file_reservation(s){extra}.")
        project_id = project.id
        async with get_session() as session:
            existing_rows = await session.execute(
                select(FileReservation, Agent.name)
                .join(Agent, FileReservation.agent_id == Agent.id)
                .where(
                    FileReservation.project_id == project_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                    FileReservation.expires_ts > datetime.now(timezone.utc),
                )
            )
            existing_reservations = existing_rows.all()

        granted: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        archive = await ensure_archive(settings, project.slug)
        async with _archive_write_lock(archive):
            for path in paths:
                conflicting_holders: list[dict[str, Any]] = []
                for file_reservation_record, holder_name in existing_reservations:
                    if _file_reservations_conflict(file_reservation_record, path, exclusive, agent):
                        conflicting_holders.append(
                            {
                                "agent": holder_name,
                                "path_pattern": file_reservation_record.path_pattern,
                                "exclusive": file_reservation_record.exclusive,
                                "expires_ts": _iso(file_reservation_record.expires_ts),
                            }
                        )
                if conflicting_holders:
                    # Advisory model: still grant the file_reservation but surface conflicts
                    conflicts.append({"path": path, "holders": conflicting_holders})
                file_reservation = await _create_file_reservation(project, agent, path, exclusive, reason, ttl_seconds)
                file_reservation_payload = {
                    "id": file_reservation.id,
                    "project": project.human_key,
                    "agent": agent.name,
                    "path_pattern": file_reservation.path_pattern,
                    "exclusive": file_reservation.exclusive,
                    "reason": file_reservation.reason,
                    "created_ts": _iso(file_reservation.created_ts),
                    "expires_ts": _iso(file_reservation.expires_ts),
                }
                await write_file_reservation_record(archive, file_reservation_payload)
                granted.append(
                    {
                        "id": file_reservation.id,
                        "path_pattern": file_reservation.path_pattern,
                        "exclusive": file_reservation.exclusive,
                        "reason": file_reservation.reason,
                        "expires_ts": _iso(file_reservation.expires_ts),
                    }
                )
                existing_reservations.append((file_reservation, agent.name))
        await ctx.info(f"Issued {len(granted)} file_reservations for '{agent.name}'. Conflicts: {len(conflicts)}")
        return {"granted": granted, "conflicts": conflicts}

    @mcp.tool(name="release_file_reservations")
    @_instrument_tool("release_file_reservations", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations"}, project_arg="project_key", agent_arg="agent_name")
    async def release_file_reservations_tool(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: Optional[list[str]] = None,
        file_reservation_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """
        Release active file reservations held by an agent.

        Behavior
        --------
        - If both `paths` and `file_reservation_ids` are omitted, all active reservations for the agent are released
        - Otherwise, restricts release to matching ids and/or path patterns
        - JSON artifacts stay in Git for audit; DB records get `released_ts`

        Returns
        -------
        dict
            { released: int, released_at: iso8601 }

        Idempotency
        -----------
        - Safe to call repeatedly. Releasing an already-released (or non-existent) reservation is a no-op.

        Examples
        --------
        Release all active reservations for agent:
        ```json
        {"jsonrpc":"2.0","id":"13","method":"tools/call","params":{"name":"release_file_reservations","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle"
        }}}
        ```

        Release by ids:
        ```json
        {"jsonrpc":"2.0","id":"14","method":"tools/call","params":{"name":"release_file_reservations","arguments":{
          "project_key":"/abs/path/backend","agent_name":"GreenCastle","file_reservation_ids":[101,102]
        }}}
        ```
        """
        if get_settings().tools_log_enabled:
            try:
                from rich.console import Console  # type: ignore
                from rich.panel import Panel  # type: ignore

                details = [
                    f"project={project_key}",
                    f"agent={agent_name}",
                    f"paths={len(paths or [])}",
                    f"ids={len(file_reservation_ids or [])}",
                ]
                Console().print(Panel.fit("\n".join(details), title="tool: release_file_reservations", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _get_agent(project, agent_name)
            if project.id is None or agent.id is None:
                raise ValueError("Project and agent must have ids before releasing file_reservations.")
            await ensure_schema()
            now = datetime.now(timezone.utc)
            async with get_session() as session:
                stmt = (
                    update(FileReservation)
                    .where(
                        FileReservation.project_id == project.id,
                        FileReservation.agent_id == agent.id,
                        cast(Any, FileReservation.released_ts).is_(None),
                    )
                    .values(released_ts=now)
                )
                if file_reservation_ids:
                    stmt = stmt.where(cast(Any, FileReservation.id).in_(file_reservation_ids))
                if paths:
                    stmt = stmt.where(cast(Any, FileReservation.path_pattern).in_(paths))
                result = await session.execute(stmt)
                await session.commit()
            affected = int(result.rowcount or 0)
            await ctx.info(f"Released {affected} file_reservations for '{agent.name}'.")
            return {"released": affected, "released_at": _iso(now)}
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    import importlib as _imp
                    _rc = _imp.import_module("rich.console")
                    _rj = _imp.import_module("rich.json")
                    Console = _rc.Console
                    JSON = _rj.JSON
                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="force_release_file_reservation")
    @_instrument_tool(
        "force_release_file_reservation",
        cluster=CLUSTER_FILE_RESERVATIONS,
        capabilities={"file_reservations", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def force_release_file_reservation(
        ctx: Context,
        project_key: str,
        agent_name: str,
        file_reservation_id: int,
        notify_previous: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        """
        Force-release a stale file reservation held by another agent after inactivity heuristics.

        The tool validates that the reservation appears abandoned (agent inactive beyond threshold and
        no recent mail/filesystem/git activity). When released, an optional notification is sent to the
        previous holder summarizing the heuristics.
        """
        project = await _get_project_by_identifier(project_key)
        actor = await _get_agent(project, agent_name)
        if project.id is None:
            raise ValueError("Project must have an id before releasing file_reservations.")

        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                select(FileReservation, Agent)
                .join(Agent, FileReservation.agent_id == Agent.id)
                .where(
                    FileReservation.id == file_reservation_id,
                    FileReservation.project_id == project.id,
                )
            )
            row = result.first()
        if not row:
            raise ToolExecutionError(
                "NOT_FOUND",
                f"File reservation id={file_reservation_id} not found for project '{project.human_key}'.",
                recoverable=True,
                data={"file_reservation_id": file_reservation_id},
            )

        reservation, holder = row
        if reservation.released_ts is not None:
            return {
                "released": 0,
                "released_at": _iso(reservation.released_ts),
                "already_released": True,
            }

        statuses = await _collect_file_reservation_statuses(project, include_released=False)
        target_status = next((status for status in statuses if status.reservation.id == reservation.id), None)
        if target_status is None:
            raise ToolExecutionError(
                "NOT_FOUND",
                "Unable to evaluate reservation status; it may have been released concurrently.",
                recoverable=True,
                data={"file_reservation_id": file_reservation_id},
            )

        if not target_status.stale:
            raise ToolExecutionError(
                "RESERVATION_ACTIVE",
                "Reservation still shows recent activity; refusing forced release.",
                recoverable=True,
                data={
                    "file_reservation_id": file_reservation_id,
                    "stale_reasons": target_status.stale_reasons,
                },
            )

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            await session.execute(
                update(FileReservation)
                .where(
                    FileReservation.id == file_reservation_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .values(released_ts=now)
            )
            await session.commit()

        reservation.released_ts = now
        settings = get_settings()
        grace_seconds = int(settings.file_reservation_activity_grace_seconds)
        inactivity_seconds = int(settings.file_reservation_inactivity_seconds)

        summary = {
            "id": reservation.id,
            "agent": holder.name,
            "path_pattern": reservation.path_pattern,
            "exclusive": reservation.exclusive,
            "reason": reservation.reason,
            "created_ts": _iso(reservation.created_ts),
            "expires_ts": _iso(reservation.expires_ts),
            "released_ts": _iso(reservation.released_ts),
            "stale_reasons": target_status.stale_reasons,
            "last_agent_activity_ts": _iso(target_status.last_agent_activity) if target_status.last_agent_activity else None,
            "last_mail_activity_ts": _iso(target_status.last_mail_activity) if target_status.last_mail_activity else None,
            "last_filesystem_activity_ts": _iso(target_status.last_fs_activity) if target_status.last_fs_activity else None,
            "last_git_activity_ts": _iso(target_status.last_git_activity) if target_status.last_git_activity else None,
        }

        await ctx.info(
            f"Force released reservation {file_reservation_id} held by '{holder.name}' on '{reservation.path_pattern}'."
        )

        notified = False
        if notify_previous and holder.name != actor.name:
            reasons_md = "\n".join(f"- {reason}" for reason in target_status.stale_reasons)
            extras: list[str] = []
            if target_status.last_agent_activity:
                delta = now - target_status.last_agent_activity
                extras.append(f"last agent activity ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_mail_activity:
                delta = now - target_status.last_mail_activity
                extras.append(f"last mail activity ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_fs_activity:
                delta = now - target_status.last_fs_activity
                extras.append(f"last filesystem touch ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_git_activity:
                delta = now - target_status.last_git_activity
                extras.append(f"last git commit ≈ {int(delta.total_seconds() // 60)} minutes ago")
            extras.append(f"inactivity threshold={inactivity_seconds}s grace={grace_seconds}s")
            extra_md = "\n".join(f"- {line}" for line in extras if line)
            body_lines = [
                f"Hi {holder.name},",
                "",
                f"I released your file reservation on `{reservation.path_pattern}` because it looked abandoned.",
                "",
                "Observed signals:",
                reasons_md or "- (none)",
            ]
            if extra_md:
                body_lines.extend(["", "Details:", extra_md])
            if note:
                body_lines.extend(["", f"Additional note from {actor.name}:", note.strip()])
            body_lines.extend(
                [
                    "",
                    "If you still need this reservation, please re-acquire it via `file_reservation_paths`.",
                ]
            )
            try:
                from fastmcp.tools.tool import FunctionTool

                send_tool = cast(FunctionTool, cast(Any, send_message))
                await send_tool.run(
                    {
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [holder.name],
                        "subject": f"[file-reservations] Released stale lock on {reservation.path_pattern}",
                        "body_md": "\n".join(body_lines),
                    }
                )
                notified = True
            except Exception:
                notified = False

        summary["notified"] = notified
        return {"released": 1, "released_at": _iso(now), "reservation": summary}
    @mcp.tool(name="renew_file_reservations")
    @_instrument_tool("renew_file_reservations", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations"}, project_arg="project_key", agent_arg="agent_name")
    async def renew_file_reservations(
        ctx: Context,
        project_key: str,
        agent_name: str,
        extend_seconds: int = 1800,
        paths: Optional[list[str]] = None,
        file_reservation_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """
        Extend expiry for active file reservations held by an agent without reissuing them.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        agent_name : str
            Agent identity who owns the reservations.
        extend_seconds : int
            Seconds to extend from the later of now or current expiry (min 60s).
        paths : Optional[list[str]]
            Restrict renewals to matching path patterns.
        file_reservation_ids : Optional[list[int]]
            Restrict renewals to matching reservation ids.

        Returns
        -------
        dict
            { renewed: int, file_reservations: [{id, path_pattern, old_expires_ts, new_expires_ts}] }
        """
        if get_settings().tools_log_enabled:
            try:
                from rich.console import Console  # type: ignore
                from rich.panel import Panel  # type: ignore

                meta = [
                    f"project={project_key}",
                    f"agent={agent_name}",
                    f"extend={extend_seconds}s",
                    f"paths={len(paths or [])}",
                    f"ids={len(file_reservation_ids or [])}",
                ]
                Console().print(Panel.fit("\n".join(meta), title="tool: renew_file_reservations", border_style="green"))
            except Exception:
                pass
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before renewing file_reservations.")
        await ensure_schema()
        now = datetime.now(timezone.utc)
        bump = max(60, int(extend_seconds))

        async with get_session() as session:
            stmt = (
                select(FileReservation)
                .where(
                    FileReservation.project_id == project.id,
                    FileReservation.agent_id == agent.id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .order_by(asc(FileReservation.expires_ts))
            )
            if file_reservation_ids:
                stmt = stmt.where(cast(Any, FileReservation.id).in_(file_reservation_ids))
            if paths:
                stmt = stmt.where(cast(Any, FileReservation.path_pattern).in_(paths))
            result = await session.execute(stmt)
            file_reservations: list[FileReservation] = list(result.scalars().all())

        if not file_reservations:
            await ctx.info(f"No active file_reservations to renew for '{agent.name}'.")
            return {"renewed": 0, "file_reservations": []}

        updated: list[dict[str, Any]] = []
        async with get_session() as session:
            for file_reservation in file_reservations:
                old_exp = file_reservation.expires_ts
                if getattr(old_exp, "tzinfo", None) is None:
                    from datetime import timezone as _tz
                    old_exp = old_exp.replace(tzinfo=_tz.utc)
                base = old_exp if old_exp > now else now
                file_reservation.expires_ts = base + timedelta(seconds=bump)
                session.add(file_reservation)
                updated.append(
                    {
                        "id": file_reservation.id,
                        "path_pattern": file_reservation.path_pattern,
                        "old_expires_ts": _iso(old_exp),
                        "new_expires_ts": _iso(file_reservation.expires_ts),
                    }
                )
            await session.commit()

        # Update Git artifacts for the renewed file_reservations
        archive = await ensure_archive(settings, project.slug)
        async with _archive_write_lock(archive):
            for file_reservation_info in updated:
                payload = {
                    "id": file_reservation_info["id"],
                    "project": project.human_key,
                    "agent": agent.name,
                    "path_pattern": file_reservation_info["path_pattern"],
                    "exclusive": True,
                    "reason": "renew",
                    "created_ts": _iso(now),
                    "expires_ts": file_reservation_info["new_expires_ts"],
                }
                await write_file_reservation_record(archive, payload)
        await ctx.info(f"Renewed {len(updated)} file_reservation(s) for '{agent.name}'.")
        return {"renewed": len(updated), "file_reservations": updated}

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

    @mcp.resource("resource://tooling/directory", mime_type="application/json")
    def tooling_directory_resource() -> dict[str, Any]:
        """
        Provide a clustered view of exposed MCP tools to combat option overload.

        The directory groups tools by workflow, outlines primary use cases,
        highlights nearby alternatives, and shares starter playbooks so agents
        can focus on the verbs relevant to their immediate task.
        """

        clusters = [
            {
                "name": "Infrastructure & Workspace Setup",
                "purpose": "Bootstrap coordination and guardrails before agents begin editing.",
                "tools": [
                    {
                        "name": "health_check",
                        "summary": "Report environment and HTTP wiring so orchestrators confirm connectivity.",
                        "use_when": "Beginning a session or during incident response triage.",
                        "related": ["ensure_project"],
                        "expected_frequency": "Once per agent session or when connectivity is in doubt.",
                        "required_capabilities": ["infrastructure"],
                        "usage_examples": [{"hint": "Pre-flight", "sample": "health_check()"}],
                    },
                    {
                        "name": "ensure_project",
                        "summary": "Ensure project slug, schema, and archive exist for a shared repo identifier.",
                        "use_when": "First call against a repo or when switching projects.",
                        "related": ["register_agent", "file_reservation_paths"],
                        "expected_frequency": "Whenever a new repo/path is encountered.",
                        "required_capabilities": ["infrastructure", "storage"],
                        "usage_examples": [{"hint": "First action", "sample": "ensure_project(human_key='/abs/path/backend')"}],
                    },
                    {
                        "name": "install_precommit_guard",
                        "summary": "Install Git pre-commit hook that enforces advisory file_reservations locally.",
                        "use_when": "Onboarding a repository into coordinated mode.",
                        "related": ["file_reservation_paths", "uninstall_precommit_guard"],
                        "expected_frequency": "Infrequent—per repository setup.",
                        "required_capabilities": ["repository", "filesystem"],
                        "usage_examples": [{"hint": "Onboard", "sample": "install_precommit_guard(project_key='backend', code_repo_path='~/repo')"}],
                    },
                    {
                        "name": "uninstall_precommit_guard",
                        "summary": "Remove the advisory pre-commit hook from a repo.",
                        "use_when": "Decommissioning or debugging the guard hook.",
                        "related": ["install_precommit_guard"],
                        "expected_frequency": "Rare; only when disabling guard enforcement.",
                        "required_capabilities": ["repository"],
                        "usage_examples": [{"hint": "Cleanup", "sample": "uninstall_precommit_guard(code_repo_path='~/repo')"}],
                    },
                ],
            },
            {
                "name": "Identity & Directory",
                "purpose": "Register agents, mint unique identities, and inspect directory metadata.",
                "tools": [
                    {
                        "name": "register_agent",
                        "summary": "Upsert an agent profile and refresh last_active_ts for a known persona.",
                        "use_when": "Resuming an identity or updating program/model/task metadata.",
                        "related": ["create_agent_identity", "whois"],
                        "expected_frequency": "At the start of each automated work session.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "Resume persona", "sample": "register_agent(project_key='/abs/path/backend', program='codex', model='gpt5')"}],
                    },
                    {
                        "name": "create_agent_identity",
                        "summary": "Always create a new unique agent name (optionally using a sanitized hint).",
                        "use_when": "Spawning a brand-new helper that should not overwrite existing profiles.",
                        "related": ["register_agent"],
                        "expected_frequency": "When minting fresh, short-lived identities.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "New helper", "sample": "create_agent_identity(project_key='backend', name_hint='GreenCastle', program='codex', model='gpt5')"}],
                    },
                    {
                        "name": "whois",
                        "summary": "Return enriched profile info plus recent archive commits for an agent.",
                        "use_when": "Dashboarding, routing coordination messages, or auditing activity.",
                        "related": ["register_agent"],
                        "expected_frequency": "Ad hoc when context about an agent is required.",
                        "required_capabilities": ["identity", "audit"],
                        "usage_examples": [{"hint": "Directory lookup", "sample": "whois(project_key='backend', agent_name='BlueLake')"}],
                    },
                    {
                        "name": "set_contact_policy",
                        "summary": "Set inbound contact policy (open, auto, contacts_only, block_all).",
                        "use_when": "Adjusting how permissive an agent is about unsolicited messages.",
                        "related": ["request_contact", "respond_contact"],
                        "expected_frequency": "Occasional configuration change.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Restrict inbox", "sample": "set_contact_policy(project_key='backend', agent_name='BlueLake', policy='contacts_only')"}],
                    },
                ],
            },
            {
                "name": "Messaging Lifecycle",
                "purpose": "Send, receive, and acknowledge threaded Markdown mail.",
                "tools": [
                    {
                        "name": "send_message",
                        "summary": "Deliver a new message with attachments, WebP conversion, and policy enforcement.",
                        "use_when": "Starting new threads or broadcasting plans across projects.",
                        "related": ["reply_message", "request_contact"],
                        "expected_frequency": "Frequent—core write operation.",
                        "required_capabilities": ["messaging"],
                        "usage_examples": [{"hint": "New plan", "sample": "send_message(project_key='backend', sender_name='GreenCastle', to=['BlueLake'], subject='Plan', body_md='...')"}],
                    },
                    {
                        "name": "reply_message",
                        "summary": "Reply within an existing thread, inheriting flags and default recipients.",
                        "use_when": "Continuing discussions or acknowledging decisions.",
                        "related": ["send_message"],
                        "expected_frequency": "Frequent when collaborating inside a thread.",
                        "required_capabilities": ["messaging"],
                        "usage_examples": [{"hint": "Thread reply", "sample": "reply_message(project_key='backend', message_id=42, sender_name='BlueLake', body_md='Got it!')"}],
                    },
                    {
                        "name": "fetch_inbox",
                        "summary": "Poll recent messages for an agent with filters (urgent_only, since_ts).",
                        "use_when": "After each work unit to ingest coordination updates.",
                        "related": ["mark_message_read", "acknowledge_message"],
                        "expected_frequency": "Frequent polling in agent loops.",
                        "required_capabilities": ["messaging", "read"],
                        "usage_examples": [{"hint": "Poll", "sample": "fetch_inbox(project_key='backend', agent_name='BlueLake', since_ts='2025-10-24T00:00:00Z')"}],
                    },
                    {
                        "name": "mark_message_read",
                        "summary": "Record read_ts for FYI messages without sending acknowledgements.",
                        "use_when": "Clearing inbox notifications once reviewed.",
                        "related": ["acknowledge_message"],
                        "expected_frequency": "Whenever FYI mail is processed.",
                        "required_capabilities": ["messaging", "read"],
                        "usage_examples": [{"hint": "Read receipt", "sample": "mark_message_read(project_key='backend', agent_name='BlueLake', message_id=42)"}],
                    },
                    {
                        "name": "acknowledge_message",
                        "summary": "Set read_ts and ack_ts so senders know action items landed.",
                        "use_when": "Responding to ack_required messages.",
                        "related": ["mark_message_read"],
                        "expected_frequency": "Each time a message requests acknowledgement.",
                        "required_capabilities": ["messaging", "ack"],
                        "usage_examples": [{"hint": "Ack", "sample": "acknowledge_message(project_key='backend', agent_name='BlueLake', message_id=42)"}],
                    },
                ],
            },
            {
                "name": "Contact Governance",
                "purpose": "Manage messaging permissions when policies are not open by default.",
                "tools": [
                    {
                        "name": "request_contact",
                        "summary": "Create or refresh a pending AgentLink and notify the target with ack_required intro.",
                        "use_when": "Requesting permission before messaging another agent.",
                        "related": ["respond_contact", "set_contact_policy"],
                        "expected_frequency": "Occasional—when new communication lines are needed.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Ask permission", "sample": "request_contact(project_key='backend', from_agent='OpsBot', to_agent='BlueLake')"}],
                    },
                    {
                        "name": "respond_contact",
                        "summary": "Approve or block a pending contact request, optionally setting expiry.",
                        "use_when": "Granting or revoking messaging permissions.",
                        "related": ["request_contact"],
                        "expected_frequency": "As often as requests arrive.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Approve", "sample": "respond_contact(project_key='backend', to_agent='BlueLake', from_agent='OpsBot', accept=True)"}],
                    },
                    {
                        "name": "list_contacts",
                        "summary": "List outbound contact links, statuses, and expirations for an agent.",
                        "use_when": "Auditing who an agent may message or rotating expiring approvals.",
                        "related": ["request_contact", "respond_contact"],
                        "expected_frequency": "Periodic audits or dashboards.",
                        "required_capabilities": ["contact", "audit"],
                        "usage_examples": [{"hint": "Audit", "sample": "list_contacts(project_key='backend', agent_name='BlueLake')"}],
                    },
                ],
            },
            {
                "name": "Search & Summaries",
                "purpose": "Surface signal from large mailboxes and compress long threads.",
                "tools": [
                    {
                        "name": "search_messages",
                        "summary": "Run FTS5 queries across subject/body text to locate relevant threads.",
                        "use_when": "Triage or gathering context before editing.",
                        "related": ["fetch_inbox", "summarize_thread"],
                        "expected_frequency": "Regular during investigation phases.",
                        "required_capabilities": ["search"],
                        "usage_examples": [{"hint": "FTS", "sample": "search_messages(project_key='backend', query='\"build plan\" AND users', limit=20)"}],
                    },
                    {
                        "name": "summarize_thread",
                        "summary": "Extract participants, key points, and action items for a single thread.",
                        "use_when": "Briefing new agents on long discussions or closing loops.",
                        "related": ["summarize_threads"],
                        "expected_frequency": "When threads exceed quick skim length.",
                        "required_capabilities": ["search", "summarization"],
                        "usage_examples": [{"hint": "Thread brief", "sample": "summarize_thread(project_key='backend', thread_id='TKT-123', include_examples=True)"}],
                    },
                    {
                        "name": "summarize_threads",
                        "summary": "Produce a digest across multiple threads with aggregate mentions/actions.",
                        "use_when": "Daily standups or cross-team sync summaries.",
                        "related": ["summarize_thread"],
                        "expected_frequency": "At cadence checkpoints (daily/weekly).",
                        "required_capabilities": ["search", "summarization"],
                        "usage_examples": [{"hint": "Digest", "sample": "summarize_threads(project_key='backend', thread_ids=['TKT-123','UX-42'])"}],
                    },
                ],
            },
            {
                "name": "File Reservations & Workspace Guardrails",
                "purpose": "Coordinate file/glob ownership to avoid overwriting concurrent work.",
                "tools": [
                    {
                        "name": "file_reservation_paths",
                        "summary": "Issue advisory file_reservations with overlap detection and Git artifacts.",
                        "use_when": "Before touching high-traffic surfaces or long-lived refactors.",
                        "related": ["release_file_reservations", "renew_file_reservations"],
                        "expected_frequency": "Whenever starting work on contested surfaces.",
                        "required_capabilities": ["file_reservations", "repository"],
                        "usage_examples": [{"hint": "Lock file", "sample": "file_reservation_paths(project_key='backend', agent_name='BlueLake', paths=['src/app.py'], ttl_seconds=7200)"}],
                    },
                    {
                        "name": "release_file_reservations",
                        "summary": "Release active file_reservations (fully or by subset) and stamp released_ts.",
                        "use_when": "Finishing work so surfaces become available again.",
                        "related": ["file_reservation_paths", "renew_file_reservations"],
                        "expected_frequency": "Each time work on a surface completes.",
                        "required_capabilities": ["file_reservations"],
                        "usage_examples": [{"hint": "Unlock", "sample": "release_file_reservations(project_key='backend', agent_name='BlueLake', paths=['src/app.py'])"}],
                    },
                    {
                        "name": "renew_file_reservations",
                        "summary": "Extend file_reservation expiry windows without allocating new file_reservation IDs.",
                        "use_when": "Long-running work needs more time but should retain ownership.",
                        "related": ["file_reservation_paths", "release_file_reservations"],
                        "expected_frequency": "Periodically during multi-hour work items.",
                        "required_capabilities": ["file_reservations"],
                        "usage_examples": [{"hint": "Extend", "sample": "renew_file_reservations(project_key='backend', agent_name='BlueLake', extend_seconds=1800)"}],
                    },
                ],
            },
            {
                "name": "Workflow Macros",
                "purpose": "Opinionated orchestrations that compose multiple primitives for smaller agents.",
                "tools": [
                    {
                        "name": "macro_start_session",
                        "summary": "Ensure project, register/update agent, optionally file_reservation surfaces, and return inbox context.",
                        "use_when": "Kickstarting a focused work session with one call.",
                        "related": ["ensure_project", "register_agent", "file_reservation_paths", "fetch_inbox"],
                        "expected_frequency": "At the beginning of each autonomous session.",
                        "required_capabilities": ["workflow", "messaging", "file_reservations", "identity"],
                        "usage_examples": [{"hint": "Bootstrap", "sample": "macro_start_session(human_key='/abs/path/backend', program='codex', model='gpt5', file_reservation_paths=['src/api/*.py'])"}],
                    },
                    {
                        "name": "macro_prepare_thread",
                        "summary": "Register or refresh an agent, summarise a thread, and fetch inbox context in one call.",
                        "use_when": "Briefing a helper before joining an ongoing discussion.",
                        "related": ["register_agent", "summarize_thread", "fetch_inbox"],
                        "expected_frequency": "Whenever onboarding a new contributor to an active thread.",
                        "required_capabilities": ["workflow", "messaging", "summarization"],
                        "usage_examples": [{"hint": "Join thread", "sample": "macro_prepare_thread(project_key='backend', thread_id='TKT-123', program='codex', model='gpt5', agent_name='ThreadHelper')"}],
                    },
                    {
                        "name": "macro_file_reservation_cycle",
                        "summary": "FileReservation a set of paths and optionally release them once work is complete.",
                        "use_when": "Wrapping a focused edit cycle that needs advisory locks.",
                        "related": ["file_reservation_paths", "release_file_reservations", "renew_file_reservations"],
                        "expected_frequency": "Per guarded work block.",
                        "required_capabilities": ["workflow", "file_reservations", "repository"],
                        "usage_examples": [{"hint": "FileReservation & release", "sample": "macro_file_reservation_cycle(project_key='backend', agent_name='BlueLake', paths=['src/app.py'], auto_release=true)"}],
                    },
                    {
                        "name": "macro_contact_handshake",
                        "summary": "Request contact approval, optionally auto-accept, and send a welcome message.",
                        "use_when": "Spinning up collaboration between two agents who lack permissions.",
                        "related": ["request_contact", "respond_contact", "send_message"],
                        "expected_frequency": "When onboarding new agent pairs.",
                        "required_capabilities": ["workflow", "contact", "messaging"],
                        "usage_examples": [{"hint": "Automated handshake", "sample": "macro_contact_handshake(project_key='backend', requester='OpsBot', target='BlueLake', auto_accept=true, welcome_subject='Hello', welcome_body='Excited to collaborate!')"}],
                    },
                ],
            },
        ]

        for cluster in clusters:
            for tool_entry in cluster["tools"]:
                tool_dict = cast(dict[str, Any], tool_entry)
                meta = TOOL_METADATA.get(str(tool_dict.get("name", "")))
                if not meta:
                    continue
                tool_dict["capabilities"] = meta["capabilities"]
                tool_dict.setdefault("complexity", meta["complexity"])
                if "required_capabilities" in tool_dict:
                    tool_dict["required_capabilities"] = meta["capabilities"]

        playbooks = [
            {
                "workflow": "Kick off new agent session (macro)",
                "sequence": ["health_check", "macro_start_session", "summarize_thread"],
            },
            {
                "workflow": "Kick off new agent session (manual)",
                "sequence": ["health_check", "ensure_project", "register_agent", "fetch_inbox"],
            },
            {
                "workflow": "Start focused refactor",
                "sequence": ["ensure_project", "file_reservation_paths", "send_message", "fetch_inbox", "acknowledge_message"],
            },
            {
                "workflow": "Join existing discussion",
                "sequence": ["macro_prepare_thread", "reply_message", "acknowledge_message"],
            },
            {
                "workflow": "Manage contact approvals",
                "sequence": ["set_contact_policy", "request_contact", "respond_contact", "send_message"],
            },
        ]

        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "metrics_uri": "resource://tooling/metrics",
            "clusters": clusters,
            "playbooks": playbooks,
        }

    @mcp.resource("resource://tooling/schemas", mime_type="application/json")
    def tooling_schemas_resource() -> dict[str, Any]:
        """Expose JSON-like parameter schemas for tools/macros to prevent drift.

        This is a lightweight, hand-maintained view focusing on the most error-prone
        parameters and accepted aliases to guide clients.
        """
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "tools": {
                "send_message": {
                    "required": ["project_key", "sender_name", "to", "subject", "body_md"],
                    "optional": ["cc", "bcc", "attachment_paths", "convert_images", "importance", "ack_required", "thread_id", "auto_contact_if_blocked"],
                    "shapes": {
                        "to": "list[str]",
                        "cc": "list[str] | str",
                        "bcc": "list[str] | str",
                        "importance": "low|normal|high|urgent",
                        "auto_contact_if_blocked": "bool",
                    },
                },
                "macro_contact_handshake": {
                    "required": ["project_key", "requester|agent_name", "target|to_agent"],
                    "optional": ["reason", "ttl_seconds", "auto_accept", "welcome_subject", "welcome_body"],
                    "aliases": {
                        "requester": ["agent_name"],
                        "target": ["to_agent"],
                    },
                },
            },
        }

    @mcp.resource("resource://tooling/metrics", mime_type="application/json")
    def tooling_metrics_resource() -> dict[str, Any]:
        """Expose aggregated tool call/error counts for analysis."""
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "tools": _tool_metrics_snapshot(),
        }

    @mcp.resource("resource://tooling/locks", mime_type="application/json")
    def tooling_locks_resource() -> dict[str, Any]:
        """Return lock metadata from the shared archive storage."""

        settings_local = get_settings()
        return collect_lock_status(settings_local)

    @mcp.resource("resource://tooling/capabilities/{agent}", mime_type="application/json")
    def tooling_capabilities_resource(agent: str, project: Optional[str] = None) -> dict[str, Any]:
        # Parse query embedded in agent path if present (robust to FastMCP variants)
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
            except Exception:
                pass
        caps = _capabilities_for(agent, project)
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "agent": agent,
            "project": project,
            "capabilities": caps,
        }

    @mcp.resource("resource://tooling/recent/{window_seconds}", mime_type="application/json")
    def tooling_recent_resource(
        window_seconds: str,
        agent: Optional[str] = None,
        project: Optional[str] = None,
    ) -> dict[str, Any]:
        # Allow query string to be embedded in the path segment per some transports
        if "?" in window_seconds:
            seg, _, qs = window_seconds.partition("?")
            window_seconds = seg
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                agent = agent or (parsed.get("agent") or [None])[0]
                project = project or (parsed.get("project") or [None])[0]
            except Exception:
                pass
        try:
            win = int(window_seconds)
        except Exception:
            win = 60
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, win))
        entries: list[dict[str, Any]] = []
        for ts, tool_name, proj, ag in list(RECENT_TOOL_USAGE):
            if ts < cutoff:
                continue
            if project and proj != project:
                continue
            if agent and ag != agent:
                continue

            record = {
                "timestamp": _iso(ts),
                "tool": tool_name,
                "project": proj,
                "agent": ag,
                "cluster": TOOL_CLUSTER_MAP.get(tool_name, "unclassified"),
            }
            entries.append(record)
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "window_seconds": win,
            "count": len(entries),
            "entries": entries,
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
        settings = get_settings()
        await ensure_schema(settings)
        # Build ignore matcher for test/demo projects
        import fnmatch as _fnmatch
        ignore_patterns = set(getattr(settings, "retention_ignore_project_patterns", []) or [])
        async with get_session() as session:
            result = await session.execute(select(Project).order_by(asc(Project.created_at)))
            projects = result.scalars().all()
            def _is_ignored(name: str) -> bool:
                return any(_fnmatch.fnmatch(name, pat) for pat in ignore_patterns)
            filtered = [p for p in projects if not (_is_ignored(p.slug) or _is_ignored(p.human_key))]
            return [_project_to_dict(project) for project in filtered]

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

    @mcp.resource("resource://agents/{project_key}", mime_type="application/json")
    async def agents_directory(project_key: str) -> dict[str, Any]:
        """
        List all registered agents in a project for easy agent discovery.

        This is the recommended way to discover other agents working on a project.

        When to use
        -----------
        - At the start of a coding session to see who else is working on the project.
        - Before sending messages to discover available recipients.
        - To check if a specific agent is registered before attempting contact.

        Parameters
        ----------
        project_key : str
            Project slug or human key (both work).

        Returns
        -------
        dict
            {
              "project": { "slug": "...", "human_key": "..." },
              "agents": [
                {
                  "name": "BackendDev",
                  "program": "claude-code",
                  "model": "sonnet-4.5",
                  "task_description": "API development",
                  "inception_ts": "2025-10-25T...",
                  "last_active_ts": "2025-10-25T...",
                  "unread_count": 3
                },
                ...
              ]
            }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r5","method":"resources/read","params":{"uri":"resource://agents/backend-abc123"}}
        ```

        Notes
        -----
        - Agent names are NOT the same as your program name or user name.
        - Use the returned names when calling tools like whois(), request_contact(), send_message().
        - Agents in different projects cannot see each other - project isolation is enforced.
        """
        project = await _get_project_by_identifier(project_key)
        await ensure_schema()

        async with get_session() as session:
            # Get all agents in the project
            result = await session.execute(
                select(Agent).where(Agent.project_id == project.id).order_by(desc(Agent.last_active_ts))
            )
            agents = result.scalars().all()

            # Get unread message counts for all agents in one query
            unread_counts_stmt = (
                select(
                    MessageRecipient.agent_id,
                    func.count(MessageRecipient.message_id).label("unread_count")
                )
                .where(
                    cast(Any, MessageRecipient.read_ts).is_(None),
                    cast(Any, MessageRecipient.agent_id).in_([agent.id for agent in agents])
                )
                .group_by(MessageRecipient.agent_id)
            )
            unread_counts_result = await session.execute(unread_counts_stmt)
            unread_counts_map = {row.agent_id: row.unread_count for row in unread_counts_result}

            # Build agent data with unread counts
            agent_data = []
            for agent in agents:
                agent_dict = _agent_to_dict(agent)
                agent_dict["unread_count"] = unread_counts_map.get(agent.id, 0)
                agent_data.append(agent_dict)

        return {
            "project": {
                "slug": project.slug,
                "human_key": project.human_key,
            },
            "agents": agent_data,
        }

    @mcp.resource("resource://file_reservations/{slug}", mime_type="application/json")
    async def file_reservations_resource(slug: str, active_only: bool = False) -> list[dict[str, Any]]:
        """
        List file_reservations for a project, optionally filtering to active-only.

        Why this exists
        ---------------
        - File reservations communicate edit intent and reduce collisions across agents.
        - Surfacing them helps humans review ongoing work and resolve contention.

        Parameters
        ----------
        slug : str
            Project slug or human key.
        active_only : bool
            If true (default), only returns file_reservations with no `released_ts`.

        Returns
        -------
        list[dict]
            Each file_reservation with { id, agent, path_pattern, exclusive, reason, created_ts, expires_ts, released_ts }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r4","method":"resources/read","params":{"uri":"resource://file_reservations/backend-abc123?active_only=true"}}
        ```

        Also see all historical (including released) file_reservations:
        ```json
        {"jsonrpc":"2.0","id":"r4b","method":"resources/read","params":{"uri":"resource://file_reservations/backend-abc123?active_only=false"}}
        ```
        """
        slug_value, query_params = _split_slug_and_query(slug)
        if "active_only" in query_params:
            active_only = _coerce_flag_to_bool(query_params["active_only"], default=active_only)

        project = await _get_project_by_identifier(slug_value)
        await ensure_schema()
        if project.id is None:
            raise ValueError("Project must have an id before listing file_reservations.")

        await _expire_stale_file_reservations(project.id)
        statuses = await _collect_file_reservation_statuses(project, include_released=not active_only)

        payload: list[dict[str, Any]] = []
        for status in statuses:
            reservation = status.reservation
            if active_only and reservation.released_ts is not None:
                continue
            payload.append(
                {
                    "id": reservation.id,
                    "agent": status.agent.name,
                    "path_pattern": reservation.path_pattern,
                    "exclusive": reservation.exclusive,
                    "reason": reservation.reason,
                    "created_ts": _iso(reservation.created_ts),
                    "expires_ts": _iso(reservation.expires_ts),
                    "released_ts": _iso(reservation.released_ts) if reservation.released_ts else None,
                    "stale": status.stale,
                    "stale_reasons": status.stale_reasons,
                    "last_agent_activity_ts": _iso(status.last_agent_activity) if status.last_agent_activity else None,
                    "last_mail_activity_ts": _iso(status.last_mail_activity) if status.last_mail_activity else None,
                    "last_filesystem_activity_ts": _iso(status.last_fs_activity) if status.last_fs_activity else None,
                    "last_git_activity_ts": _iso(status.last_git_activity) if status.last_git_activity else None,
                }
            )
        return payload

    @mcp.resource("resource://message/{message_id}", mime_type="application/json")
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
        # Support toolkits that pass query in the template segment
        if "?" in message_id:
            id_part, _, qs = message_id.partition("?")
            message_id = id_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
            except Exception:
                pass
        if project is None:
            # Try to infer project by message id when unique
            async with get_session() as s_auto:
                rows = await s_auto.execute(select(Project, Message).join(Message, Message.project_id == Project.id).where(cast(Any, Message.id) == int(message_id)).limit(2))
                data = rows.all()
            if len(data) == 1:
                project_obj = data[0][0]
            else:
                raise ValueError("project parameter is required for message resource")
        else:
            project_obj = await _get_project_by_identifier(project)
        message = await _get_message(project_obj, int(message_id))
        sender = await _get_agent_by_id(project_obj, message.sender_id)
        payload = _message_to_dict(message, include_body=True)
        payload["from"] = sender.name
        return payload

    @mcp.resource("resource://thread/{thread_id}", mime_type="application/json")
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
        # Robust query parsing: some FastMCP versions do not inject query args.
        # If the templating layer included the query string in the path segment,
        # extract it and fill missing parameters.
        if "?" in thread_id:
            id_part, _, qs = thread_id.partition("?")
            thread_id = id_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and "project" in parsed and parsed["project"]:
                    project = parsed["project"][0]
                if parsed.get("include_bodies"):
                    val = parsed["include_bodies"][0].strip().lower()
                    include_bodies = val in ("1", "true", "t", "yes", "y")
            except Exception:
                pass

        # Determine project if omitted by client
        if project is None:
            # Auto-detect project using numeric seed (message id) or unique thread key
            async with get_session() as s_auto:
                try:
                    msg_id = int(thread_id)
                except ValueError:
                    msg_id = None
                if msg_id is not None:
                    rows = await s_auto.execute(
                        select(Project)
                        .join(Message, Message.project_id == Project.id)
                        .where(cast(Any, Message.id) == msg_id)
                        .limit(2)
                    )
                    projects = [row[0] for row in rows.all()]
                else:
                    rows = await s_auto.execute(
                        select(Project)
                        .join(Message, Message.project_id == Project.id)
                        .where(Message.thread_id == thread_id)
                        .limit(2)
                    )
                    projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for thread resource")
        else:
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
        "resource://inbox/{agent}",
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
        # Robust query parsing: some FastMCP versions do not inject query args.
        # If the templating layer included the query string in the last path segment,
        # extract it and fill missing parameters.
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and "project" in parsed and parsed["project"]:
                    project = parsed["project"][0]
                if since_ts is None and "since_ts" in parsed and parsed["since_ts"]:
                    since_ts = parsed["since_ts"][0]
                if parsed.get("urgent_only"):
                    val = parsed["urgent_only"][0].strip().lower()
                    urgent_only = val in ("1", "true", "t", "yes", "y")
                if parsed.get("include_bodies"):
                    val = parsed["include_bodies"][0].strip().lower()
                    include_bodies = val in ("1", "true", "t", "yes", "y")
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            # Auto-detect project by agent name if uniquely identifiable
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for inbox resource")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        messages = await _list_inbox(project_obj, agent_obj, limit, urgent_only, include_bodies, since_ts)
        # Enrich with commit info for canonical markdown files (best-effort)
        enriched: list[dict[str, Any]] = []
        for item in messages:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        return {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "count": len(enriched),
            "messages": enriched,
        }

    @mcp.resource("resource://views/urgent-unread/{agent}", mime_type="application/json")
    async def urgent_unread_view(agent: str, project: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        """
        Convenience view listing urgent and high-importance messages that are unread for an agent.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        limit : int
            Max number of messages.
        """
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for urgent view")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        items = await _list_inbox(project_obj, agent_obj, limit, urgent_only=True, include_bodies=False, since_ts=None)
        # Filter unread (no read_ts recorded)
        unread: list[dict[str, Any]] = []
        async with get_session() as session:
            from .models import MessageRecipient  # local import to avoid cycle at top

            for item in items:
                result = await session.execute(
                    select(MessageRecipient.read_ts).where(
                        MessageRecipient.message_id == item["id"], MessageRecipient.agent_id == agent_obj.id
                    )
                )
                read_ts = result.scalar_one_or_none()
                if read_ts is None:
                    unread.append(item)
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(unread), "messages": unread[:limit]}

    @mcp.resource("resource://views/ack-required/{agent}", mime_type="application/json")
    async def ack_required_view(agent: str, project: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        """
        Convenience view listing messages requiring acknowledgement for an agent where ack is pending.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        limit : int
            Max number of messages.
        """
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for ack view")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_obj.id,
                    MessageRecipient.agent_id == agent_obj.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(desc(Message.created_ts))
                .limit(limit)
            )
            for msg, kind in rows.all():
                payload = _message_to_dict(msg, include_body=False)
                payload["kind"] = kind
                out.append(payload)
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}

    @mcp.resource("resource://views/acks-stale/{agent}", mime_type="application/json")
    async def acks_stale_view(
        agent: str,
        project: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List ack-required messages older than a TTL where acknowledgement is still missing.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        ttl_seconds : Optional[int]
            Minimum age in seconds to consider a message stale. Defaults to settings.ack_ttl_seconds.
        limit : int
            Max number of messages to return.
        """
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("ttl_seconds"):
                    with suppress(Exception):
                        ttl_seconds = int(parsed["ttl_seconds"][0])
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for stale acks view")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        ttl = int(ttl_seconds) if ttl_seconds is not None else get_settings().ack_ttl_seconds
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind, MessageRecipient.read_ts)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_obj.id,
                    MessageRecipient.agent_id == agent_obj.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(Message.created_ts))
                .limit(limit * 5)
            )
            for msg, kind, read_ts in rows.all():
                # Coerce potential naive datetimes from SQLite to UTC for arithmetic
                created = msg.created_ts
                if getattr(created, "tzinfo", None) is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_s = int((now - created).total_seconds())
                if age_s >= ttl:
                    payload = _message_to_dict(msg, include_body=False)
                    payload["kind"] = kind
                    payload["read_at"] = _iso(read_ts) if read_ts else None
                    payload["age_seconds"] = age_s
                    out.append(payload)
                    if len(out) >= limit:
                        break
        return {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "ttl_seconds": ttl,
            "count": len(out),
            "messages": out,
        }

    @mcp.resource("resource://views/ack-overdue/{agent}", mime_type="application/json")
    async def ack_overdue_view(
        agent: str,
        project: Optional[str] = None,
        ttl_minutes: int = 60,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List messages requiring acknowledgement older than ttl_minutes without ack."""
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("ttl_minutes"):
                    with suppress(Exception):
                        ttl_minutes = int(parsed["ttl_minutes"][0])
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for ack-overdue view")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, ttl_minutes))
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.project_id == project_obj.id,
                    MessageRecipient.agent_id == agent_obj.id,
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(Message.created_ts))
                .limit(limit * 5)
            )
            for msg, kind in rows.all():
                created = msg.created_ts
                if getattr(created, "tzinfo", None) is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created <= cutoff:
                    payload = _message_to_dict(msg, include_body=False)
                    payload["kind"] = kind
                    out.append(payload)
                    if len(out) >= limit:
                        break
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}

    @mcp.resource("resource://mailbox/{agent}", mime_type="application/json")
    async def mailbox_resource(agent: str, project: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        """
        List recent messages in an agent's mailbox with lightweight Git commit context.

        Returns
        -------
        dict
            { project, agent, count, messages: [{ id, subject, from, created_ts, importance, ack_required, kind, commit: {hexsha, summary} | null }] }
        """
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for mailbox resource")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        items = await _list_inbox(project_obj, agent_obj, limit, urgent_only=False, include_bodies=False, since_ts=None)

        # Attach recent commit summaries touching the archive (best-effort)
        commits_index: dict[str, dict[str, str]] = {}
        try:
            archive = await ensure_archive(settings, project_obj.slug)
            repo: Repo = archive.repo
            for commit in repo.iter_commits(paths=["."], max_count=200):
                # Heuristic: extract message id from commit summary when present in canonical subject format
                # Expected: "mail: <from> -> ... | <subject>"
                summary = str(commit.summary)
                hexsha = commit.hexsha[:12]
                if hexsha not in commits_index:
                    commits_index[hexsha] = {"hexsha": hexsha, "summary": summary}
        except Exception:
            pass

        # Map messages to nearest commit (best-effort: none if not determinable)
        out: list[dict[str, Any]] = []
        for item in items:
            commit_meta = None
            # We cannot cheaply know exact commit per message without parsing message ids from log; keep null
            # but preserve structure for clients
            if commits_index:
                commit_meta = next(iter(commits_index.values()))  # provide at least one recent reference
            payload = dict(item)
            payload["commit"] = commit_meta
            out.append(payload)
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}

    @mcp.resource(
        "resource://mailbox-with-commits/{agent}",
        mime_type="application/json",
    )
    async def mailbox_with_commits_resource(agent: str, project: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        """List recent messages in an agent's mailbox with commit metadata including diff summaries."""
        # Parse query embedded in agent path if present
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
            except Exception:
                pass
        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, Agent.project_id == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for mailbox-with-commits resource")
        else:
            project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        items = await _list_inbox(project_obj, agent_obj, limit, urgent_only=False, include_bodies=False, since_ts=None)

        enriched: list[dict[str, Any]] = []
        for item in items:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}

    @mcp.resource("resource://outbox/{agent}", mime_type="application/json")
    async def outbox_resource(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        include_bodies: bool = False,
        since_ts: Optional[str] = None,
    ) -> dict[str, Any]:
        """List messages sent by the agent, enriched with commit metadata for canonical files."""
        # Support toolkits that incorrectly pass query in the template segment
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    from contextlib import suppress
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
                if parsed.get("include_bodies"):
                    include_bodies = parsed["include_bodies"][0].lower() in {"1","true","t","yes","y"}
                if parsed.get("since_ts"):
                    since_ts = parsed["since_ts"][0]
            except Exception:
                pass
        """List messages sent by the agent, enriched with commit metadata for canonical files."""
        if project is None:
            raise ValueError("project parameter is required for outbox resource")
        project_obj = await _get_project_by_identifier(project)
        agent_obj = await _get_agent(project_obj, agent)
        items = await _list_outbox(project_obj, agent_obj, limit, include_bodies, since_ts)
        enriched: list[dict[str, Any]] = []
        for item in items:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        return {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}

    # No explicit output-schema transform; the tool returns ToolResult with {"result": ...}

    return mcp
