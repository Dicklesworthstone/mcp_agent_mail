"""Application factory for the MCP Agent Mail server."""
# ruff: noqa: I001, A002

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import hashlib
import inspect
import json
import logging
import re
import shlex
import subprocess
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Optional, Protocol, cast
from urllib.parse import parse_qsl
import uuid

from fastmcp import Context, FastMCP
from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from sqlalchemy import asc as _sa_asc, bindparam, desc as _sa_desc, func, or_ as _sa_or, select as _sa_select, text, update as _sa_update
from sqlalchemy.exc import IntegrityError, NoResultFound, OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.orm import aliased

from . import rich_logger
from .config import Settings, get_settings
from .db import (
    ensure_schema,
    get_engine,
    get_query_tracker,
    get_session,
    init_engine,
    start_query_tracking,
    stop_query_tracking,
)
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .llm import complete_system_user
from .models import (
    Agent,
    AgentLink,
    FileReservation,
    Message,
    MessageRecipient,
    Project,
    ProjectSiblingSuggestion,
    Product,
    ProductProjectLink,
)
from .storage import (
    GitIndexLockError,
    ProjectArchive,
    archive_write_lock,
    clear_notification_signal,
    clear_repo_cache,
    collect_lock_status,
    emit_notification_signal,
    ensure_archive,
    heal_archive_locks,
    process_attachments,
    write_agent_profile,
    write_file_reservation_records,
    write_message_bundle,
)
from .utils import (
    generate_agent_name,
    sanitize_agent_name,
    slugify,
    validate_agent_name_format,
    validate_thread_id_format,
)

PathSpec: Any
try:
    from pathspec import PathSpec as _PathSpec
    PathSpec = _PathSpec
except Exception:  # pragma: no cover - optional dependency fallback
    PathSpec = None

logger = logging.getLogger(__name__)


class _FastMCPToolGetter(Protocol):
    async def get_tool(self, name: str) -> Any: ...


class _ToolRegistryLike(Protocol):
    _tools: dict[str, Any]


class _FastMCPToolManagerLike(Protocol):
    _tool_manager: _ToolRegistryLike

# ty currently struggles to type SQLModel-mapped SQLAlchemy expressions.
# Provide lightweight wrappers to keep type checking focused on our code.
def select(*entities: Any, **kwargs: Any) -> Any:
    return _sa_select(*entities, **kwargs)


def update(*args: Any, **kwargs: Any) -> Any:
    return _sa_update(*args, **kwargs)


def or_(*clauses: Any) -> Any:
    return _sa_or(*clauses)


def asc(value: Any) -> Any:
    return _sa_asc(value)


def desc(value: Any) -> Any:
    return _sa_desc(value)


@contextlib.contextmanager
def _git_repo(path: str | Path, search_parent_directories: bool = True) -> Any:
    """Context manager for GitPython Repo that ensures proper cleanup.

    GitPython's Repo object opens file handles for index, config, and other files.
    Without explicit cleanup, these accumulate and cause "too many open files" errors
    under heavy load. This context manager ensures repo.close() is always called.

    Usage:
        with _git_repo("/path/to/project") as repo:
            branch = repo.active_branch.name
    """
    repo = None
    try:
        repo = Repo(path, search_parent_directories=search_parent_directories)
        yield repo
    finally:
        if repo is not None:
            with suppress(Exception):
                repo.close()

TOOL_METRICS: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "errors": 0})
TOOL_CLUSTER_MAP: dict[str, str] = {}
TOOL_METADATA: dict[str, dict[str, Any]] = {}

RECENT_TOOL_USAGE: deque[tuple[datetime, str, Optional[str], Optional[str]]] = deque(maxlen=4096)

# Tools that are safe to auto-retry after transient OS-level FD exhaustion (EMFILE).
# Keep this list conservative: do NOT include tools like send_message that can create
# duplicate side effects if re-run after a partial success.
_EMFILE_RETRY_TOOLS: frozenset[str] = frozenset(
    {
        "ensure_project",
        "register_agent",
        "create_agent_identity",
        "fetch_inbox",
        "search_messages",
        "search_messages_product",
        "list_contacts",
        "whois",
    }
)

CLUSTER_SETUP = "infrastructure"
CLUSTER_IDENTITY = "identity"
CLUSTER_MESSAGING = "messaging"
CLUSTER_CONTACT = "contact"
CLUSTER_SEARCH = "search"
CLUSTER_FILE_RESERVATIONS = "file_reservations"
CLUSTER_MACROS = "workflow_macros"
CLUSTER_BUILD_SLOTS = "build_slots"
CLUSTER_PRODUCT = "product_bus"

# -------------------------------------------------------------------------------------------------
# Tool Filtering: Predefined profiles for context reduction
# -------------------------------------------------------------------------------------------------
# Each profile maps to a set of clusters or specific tools to include.
# Using profiles can reduce context overhead by up to ~70% for minimal workflows.
#
# Profile definitions:
#   - full: All tools (default, no filtering)
#   - core: Essential tools for typical agent workflows
#   - minimal: Bare minimum for simple message passing
#   - messaging: Focus on messaging without file reservations
#   - custom: User-defined via TOOLS_FILTER_CLUSTERS/TOOLS_FILTER_TOOLS

TOOL_FILTER_PROFILES: dict[str, dict[str, list[str] | set[str]]] = {
    "full": {
        "clusters": [],  # Empty = all clusters
        "tools": [],
    },
    "core": {
        "clusters": [CLUSTER_IDENTITY, CLUSTER_MESSAGING, CLUSTER_FILE_RESERVATIONS, CLUSTER_MACROS],
        "tools": ["health_check", "ensure_project"],
    },
    "minimal": {
        "clusters": [],
        "tools": [
            "health_check",
            "ensure_project",
            "register_agent",
            "send_message",
            "fetch_inbox",
            "acknowledge_message",
        ],
    },
    "messaging": {
        "clusters": [CLUSTER_IDENTITY, CLUSTER_MESSAGING, CLUSTER_CONTACT],
        "tools": ["health_check", "ensure_project", "search_messages"],
    },
}

# Track filtered tools for logging/debugging
_FILTERED_TOOLS: set[str] = set()


def _should_expose_tool(tool_name: str, cluster: str, settings: Settings) -> bool:
    """Determine if a tool should be exposed based on filter settings.

    Returns True if the tool should be registered, False if it should be hidden.
    This is evaluated once at server startup, not per-request.
    """
    filter_cfg = settings.tool_filter
    if not filter_cfg.enabled:
        return True  # No filtering, expose all tools

    profile = filter_cfg.profile

    # Custom profile: use explicit clusters/tools from settings
    if profile == "custom":
        clusters_list = filter_cfg.clusters
        tools_list = filter_cfg.tools
        mode = filter_cfg.mode

        # If no explicit filters, expose all
        if not clusters_list and not tools_list:
            return True

        in_cluster = cluster in clusters_list if clusters_list else False
        in_tools = tool_name in tools_list if tools_list else False

        if mode == "include":
            return in_cluster or in_tools
        else:  # exclude
            return not (in_cluster or in_tools)

    # Predefined profile
    if profile == "full":
        return True

    profile_def = TOOL_FILTER_PROFILES.get(profile)
    if not profile_def:
        return True  # Unknown profile, default to exposing

    profile_clusters = profile_def.get("clusters", [])
    profile_tools = profile_def.get("tools", [])

    # If profile_clusters is empty for that profile, only check tools
    if profile_clusters and cluster in profile_clusters:
        return True
    if profile_tools and tool_name in profile_tools:
        return True

    # For profiles with explicit lists, if tool not in any list, don't expose
    return not (profile_clusters or profile_tools)


def _filtered_tool_decorator(
    mcp: FastMCP,
    tool_name: str,
    cluster: str,
    settings: Settings,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Conditional tool registration based on filter settings.

    Returns either the real @mcp.tool decorator or a no-op decorator that
    doesn't register the tool.
    """
    if _should_expose_tool(tool_name, cluster, settings):
        return mcp.tool(name=tool_name, **kwargs)
    else:
        _FILTERED_TOOLS.add(tool_name)

        # Return a no-op decorator that preserves the function but doesn't register it
        def noop_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return noop_decorator


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
) -> Callable[[Any], Any]:
    meta = {
        "cluster": cluster,
        "capabilities": sorted(capabilities or {cluster}),
        "complexity": complexity,
        "agent_arg": agent_arg,
        "project_arg": project_arg,
    }
    _register_tool(tool_name, meta)

    def decorator(func: Any) -> Any:
        signature = inspect.signature(func)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.perf_counter()

            metrics = TOOL_METRICS[tool_name]
            metrics["calls"] += 1
            bound = _bind_arguments(signature, args, kwargs)
            ctx = bound.arguments.get("ctx")
            format_value = bound.arguments.get("format")
            if isinstance(ctx, Context) and meta["capabilities"]:
                required_caps = set(cast(list[str], meta["capabilities"]))
                _enforce_capabilities(ctx, required_caps, tool_name)
            project_value = _extract_argument(bound, project_arg)
            agent_value = _extract_argument(bound, agent_arg)

            # Rich logging: Log tool call start if enabled
            settings = get_settings()
            log_enabled = settings.tools_log_enabled
            log_ctx = None
            query_tracker = get_query_tracker()
            tracker_token = None

            if query_tracker is None and settings.instrumentation_enabled:
                query_tracker, tracker_token = start_query_tracking(
                    slow_ms=float(settings.instrumentation_slow_query_ms),
                )

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
                try:
                    result = await func(*args, **kwargs)
                except OSError as exc:
                    # Best-effort recovery for EMFILE on safe/idempotent tools.
                    import errno

                    if exc.errno == errno.EMFILE and tool_name in _EMFILE_RETRY_TOOLS:
                        with suppress(Exception):
                            clear_repo_cache()
                        with suppress(Exception):
                            import gc

                            gc.collect()
                        await asyncio.sleep(0.05)
                        result = await func(*args, **kwargs)
                    else:
                        raise
                if format_value is not None or settings.output_format_default or settings.toon_default_format:
                    result = await _apply_tool_output_format(
                        result,
                        ctx=ctx if isinstance(ctx, Context) else None,
                        tool_name=tool_name,
                        settings=settings,
                        format_value=format_value,
                    )
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
            except ValueError as exc:
                # Invalid argument value
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "INVALID_ARGUMENT",
                    f"Invalid argument value: {exc}. Check that all parameters have valid values.",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except TypeError as exc:
                # Wrong argument type
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error_msg = str(exc)
                # Try to extract helpful info from TypeError
                hint = ""
                if "got an unexpected keyword argument" in error_msg:
                    hint = " Check parameter names for typos."
                elif "missing" in error_msg and "required" in error_msg:
                    hint = " Ensure all required parameters are provided."
                elif "NoneType" in error_msg:
                    hint = " A required value was None/null."
                wrapped_exc = ToolExecutionError(
                    "TYPE_ERROR",
                    f"Argument type mismatch: {exc}.{hint}",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except KeyError as exc:
                # Missing key/field
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "MISSING_FIELD",
                    f"Missing required field: {exc}. Ensure all required parameters are provided.",
                    recoverable=True,
                    data={"tool": tool_name, "missing_field": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except SATimeoutError as exc:
                # SQLAlchemy pool timeout (QueuePool exhausted)
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                db_settings = settings.database
                wrapped_exc = ToolExecutionError(
                    "DATABASE_POOL_EXHAUSTED",
                    "Database connection pool exhausted. Reduce concurrency or increase pool settings.",
                    recoverable=True,
                    data={
                        "tool": tool_name,
                        "pool_size": db_settings.pool_size,
                        "max_overflow": db_settings.max_overflow,
                        "pool_timeout": db_settings.pool_timeout,
                        "error_detail": str(exc),
                    },
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except TimeoutError as exc:
                # Timeout (database lock, network, etc.)
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "TIMEOUT",
                    f"Operation timed out: {exc}. The server may be under heavy load. Try again in a moment.",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except GitIndexLockError as exc:
                # Git index.lock contention (concurrent git operations)
                # This is an expected error in multi-agent environments
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "GIT_INDEX_LOCK",
                    f"Git repository is temporarily locked by another operation. "
                    f"This is normal in multi-agent environments. "
                    f"Wait a moment and retry. (Attempted {exc.attempts} times before giving up)",
                    recoverable=True,
                    data={
                        "tool": tool_name,
                        "lock_path": str(exc.lock_path),
                        "attempts": exc.attempts,
                    },
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except OSError as exc:
                # Handle file descriptor exhaustion (EMFILE) with cache cleanup
                import errno
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                if exc.errno == errno.EMFILE:
                    # Clear repo cache to free file handles and allow recovery
                    cleared = clear_repo_cache()
                    wrapped_exc = ToolExecutionError(
                        "RESOURCE_EXHAUSTED",
                        f"Too many open files. Freed {cleared} cached repos. Retry the operation.",
                        recoverable=True,
                        data={"tool": tool_name, "freed_repos": cleared, "error_detail": str(exc)},
                    )
                else:
                    wrapped_exc = ToolExecutionError(
                        "OS_ERROR",
                        f"OS error: {exc}",
                        recoverable=False,
                        data={"tool": tool_name, "errno": exc.errno, "error_detail": str(exc)},
                    )
                error = wrapped_exc
                raise wrapped_exc from exc
            except Exception as exc:
                # Catch-all for unexpected errors - provide helpful categorization
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error_type = type(exc).__name__
                error_msg = str(exc)

                # Try to categorize common error patterns
                if "database" in error_msg.lower() or "sqlite" in error_msg.lower():
                    error_category = "DATABASE_ERROR"
                    friendly_msg = "A database error occurred. This may be a transient issue - try again."
                    recoverable = True
                elif "lock" in error_msg.lower() or "busy" in error_msg.lower():
                    error_category = "RESOURCE_BUSY"
                    friendly_msg = "Resource is temporarily busy. Wait a moment and try again."
                    recoverable = True
                elif "permission" in error_msg.lower() or "access" in error_msg.lower():
                    error_category = "PERMISSION_ERROR"
                    friendly_msg = f"Access denied: {error_msg}"
                    recoverable = False
                elif "connection" in error_msg.lower() or "network" in error_msg.lower():
                    error_category = "CONNECTION_ERROR"
                    friendly_msg = "Connection error occurred. Check network and try again."
                    recoverable = True
                else:
                    error_category = "UNHANDLED_EXCEPTION"
                    friendly_msg = f"Unexpected error ({error_type}): {error_msg}"
                    recoverable = False

                wrapped_exc = ToolExecutionError(
                    error_category,
                    friendly_msg,
                    recoverable=recoverable,
                    data={"tool": tool_name, "original_error": error_type, "error_detail": error_msg},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            finally:
                _record_recent(tool_name, project_value, agent_value)

                query_stats = None
                if query_tracker is not None:
                    query_stats = query_tracker.to_dict()

                if query_stats and settings.instrumentation_enabled:
                    logger.info(
                        "tool_query_stats",
                        extra={
                            "tool": tool_name,
                            "project": project_value,
                            "agent": agent_value,
                            "queries": query_stats.get("total", 0),
                            "query_time_ms": query_stats.get("total_time_ms", 0.0),
                            "per_table": query_stats.get("per_table", {}),
                            "slow_query_ms": query_stats.get("slow_query_ms"),
                        },
                    )

                # Rich logging: Log tool call end if enabled
                if log_ctx is not None:
                    try:
                        log_ctx.end_time = time.perf_counter()
                        log_ctx.result = result
                        log_ctx.error = error
                        log_ctx.success = error is None
                        if query_stats:
                            log_ctx.query_stats = query_stats
                        rich_logger.log_tool_call_end(log_ctx)
                    except Exception:
                        # Logging errors should not suppress original exceptions
                        pass

                if tracker_token is not None:
                    stop_query_tracking(tracker_token)

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


def _lifespan_factory(settings: Settings) -> Callable[[FastMCP], AsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastMCP) -> AsyncIterator[None]:
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
        try:
            yield
        finally:
            cancelled: BaseException | None = None
            dispose_task: asyncio.Task[None] | None = None
            with suppress(Exception):
                engine = get_engine()
                dispose_task = asyncio.create_task(engine.dispose())
            if dispose_task is not None:
                try:
                    await asyncio.shield(dispose_task)
                except asyncio.CancelledError as exc:
                    cancelled = exc
                    with suppress(BaseException):
                        await dispose_task
                except Exception:
                    with suppress(BaseException):
                        await dispose_task
            with suppress(BaseException):
                clear_repo_cache()
            if cancelled is not None:
                raise cancelled

    return lifespan


def _iso(dt: Any) -> str:
    """Return ISO-8601 in UTC from datetime or best-effort from string.

    Accepts datetime or ISO-like string; falls back to str(dt) if unknown.
    Naive datetimes (from SQLite) are assumed to be UTC already.
    """
    try:
        if isinstance(dt, str):
            try:
                parsed = datetime.fromisoformat(dt)
                # Handle naive parsed datetimes (assume UTC)
                if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                return dt
        if hasattr(dt, "astimezone"):
            # Handle naive datetimes from SQLite (assume UTC)
            if getattr(dt, "tzinfo", None) is None or dt.tzinfo.utcoffset(dt) is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
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


def _naive_utc(dt: Optional[datetime] = None) -> datetime:
    """Return a naive UTC datetime for SQLite comparisons.

    SQLite stores datetimes without timezone info. When comparing Python
    datetime objects with SQLite DATETIME columns via SQLAlchemy, both must
    be naive to avoid 'can't compare offset-naive and offset-aware datetimes'.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        # Convert to UTC first, then strip timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    return slug, params


def _coerce_flag_to_bool(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_FLAG_VALUES:
        return True
    if normalized in _FALSE_FLAG_VALUES:
        return False
    return default


_OUTPUT_FORMAT_AUTO_VALUES: frozenset[str] = frozenset({"", "auto", "default", "none", "null"})
_OUTPUT_FORMAT_ALIASES: dict[str, str] = {
    "application/json": "json",
    "text/json": "json",
    "application/toon": "toon",
    "text/toon": "toon",
}
_TOON_STATS_TOKENS_RE = re.compile(
    "Token estimates:\\s*~(?P<json>\\d+)\\s*\\(JSON\\)\\s*(?:->|\\u2192)\\s*~(?P<toon>\\d+)\\s*\\(TOON\\)"
)
_TOON_STATS_SAVED_RE = re.compile(r"Saved\\s*~(?P<saved>\\d+)\\s*tokens\\s*\\((?P<percent>-?\\d+(?:\\.\\d+)?)%\\)")


@dataclass(frozen=True, slots=True)
class _OutputFormatDecision:
    resolved: str
    source: str
    requested: Optional[str]


def _normalize_output_format(value: Any) -> tuple[Optional[str], bool]:
    if value is None:
        return None, True
    text = str(value).strip().lower()
    if text in _OUTPUT_FORMAT_AUTO_VALUES:
        return None, True
    if text in _OUTPUT_FORMAT_ALIASES:
        text = _OUTPUT_FORMAT_ALIASES[text]
    if text in {"json", "toon"}:
        return text, True
    return None, False


def _resolve_output_format(value: Any, settings: Settings) -> _OutputFormatDecision:
    normalized, ok = _normalize_output_format(value)
    if value is not None and not ok:
        raise ValueError(f"Invalid format '{value}'. Expected 'json' or 'toon'.")
    if normalized:
        return _OutputFormatDecision(resolved=normalized, source="param", requested=normalized)

    default_raw = settings.output_format_default or settings.toon_default_format
    default_normalized, ok = _normalize_output_format(default_raw)
    if default_raw and not ok:
        logger.warning(
            "Invalid output format default; falling back to json",
            extra={"value": default_raw},
        )
    if default_normalized:
        return _OutputFormatDecision(resolved=default_normalized, source="default", requested=default_normalized)
    return _OutputFormatDecision(resolved="json", source="implicit", requested=None)


def _truncate_text(value: str, *, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(+{len(value) - limit} chars)"


@functools.lru_cache(maxsize=32)
def _looks_like_toon_rust_encoder(exe: str) -> bool:
    """
    Best-effort guardrail to prevent accidentally using non-toon_rust encoders
    (e.g. the Node.js `toon` CLI or coreutils `tr`).

    We rely on toon_rust's help/version banners, which are stable across installs.
    """
    exe_basename = exe.split("/")[-1].split("\\")[-1].lower()
    if exe_basename in {"toon", "toon.exe"}:
        # Never accept (or invoke) the Node.js `toon` CLI as the encoder backend.
        return False

    try:
        help_result = subprocess.run(
            [exe, "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return False

    help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
    if "reference implementation in rust" in help_text.lower():
        return True

    try:
        ver_result = subprocess.run(
            [exe, "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return False

    ver_text = ((ver_result.stdout or "") + (ver_result.stderr or "")).strip().lower()
    return ver_text.startswith("tru ") or ver_text.startswith("toon_rust ")


def _toon_command(settings: Settings) -> list[str]:
    raw = (settings.toon_bin or "tru").strip()
    if not raw:
        return ["tru"]
    try:
        cmd = shlex.split(raw)
    except ValueError:
        cmd = [raw]

    # Enforce toon_rust-only encoder usage (never the Node.js `toon` CLI).
    if cmd:
        exe = cmd[0]
        if not _looks_like_toon_rust_encoder(exe):
            raise ValueError(
                f"TOON_BIN resolved to {exe!r}, which does not look like toon_rust "
                f"(expected tru). Refusing to run a non-toon_rust encoder."
            )
    return cmd


def _run_toon_encode(json_payload: str, settings: Settings) -> subprocess.CompletedProcess[str]:
    cmd = [*_toon_command(settings), "--encode"]
    if settings.toon_stats_enabled:
        cmd.append("--stats")
    return subprocess.run(
        cmd,
        input=json_payload,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_toon_stats(stderr: str) -> Optional[dict[str, Any]]:
    stats: dict[str, Any] = {}
    tokens_match = _TOON_STATS_TOKENS_RE.search(stderr)
    if tokens_match:
        stats["json_tokens"] = int(tokens_match.group("json"))
        stats["toon_tokens"] = int(tokens_match.group("toon"))
    saved_match = _TOON_STATS_SAVED_RE.search(stderr)
    if saved_match:
        stats["saved_tokens"] = int(saved_match.group("saved"))
        stats["saved_percent"] = float(saved_match.group("percent"))
    return stats or None


def _json_fallback(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _dump_json_compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_fallback)


def _encode_payload_to_toon_sync(
    payload: Any,
    *,
    settings: Settings,
    tool_name: str,
    source: str,
    requested: str,
) -> dict[str, Any]:
    try:
        json_payload = _dump_json_compact(payload)
    except Exception as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"json serialization failed: {exc}",
            },
        }
    try:
        result = _run_toon_encode(json_payload, settings)
    except ValueError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": str(exc),
            },
        }
    except FileNotFoundError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder not found: {exc}",
            },
        }
    except OSError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder failed: {exc}",
            },
        }

    if result.returncode != 0:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder exited with {result.returncode}",
                "toon_stderr": _truncate_text(result.stderr or ""),
            },
        }

    toon_text = (result.stdout or "").rstrip("\n")
    try:
        encoder = _toon_command(settings)[0]
    except Exception:
        encoder = "tru"
    meta: dict[str, Any] = {
        "requested": requested,
        "source": source,
        "encoder": encoder,
    }
    stats = _parse_toon_stats(result.stderr or "")
    if stats:
        meta["toon_stats"] = stats
    elif settings.toon_stats_enabled and result.stderr:
        meta["toon_stats_raw"] = _truncate_text(result.stderr)
    return {
        "format": "toon",
        "data": toon_text,
        "meta": meta,
    }


def _extract_structured_payload(result: Any) -> tuple[Any, Optional[Callable[[Any], None]]]:
    if hasattr(result, "structured_content"):
        try:
            payload = result.structured_content

            def _setter(value: Any) -> None:
                result.structured_content = value
                if hasattr(result, "data"):
                    with suppress(Exception):
                        result.data = value

            return payload, _setter
        except Exception:
            return result, None
    if isinstance(result, dict) and "structured_content" in result:
        payload = result.get("structured_content")

        def _setter(value: Any) -> None:
            result["structured_content"] = value

        return payload, _setter
    return result, None


async def _apply_tool_output_format(
    result: Any,
    *,
    ctx: Optional[Context],
    tool_name: str,
    settings: Settings,
    format_value: Any,
) -> Any:
    decision = _resolve_output_format(format_value, settings)
    if decision.resolved != "toon":
        return result

    payload, setter = _extract_structured_payload(result)
    if payload is None:
        return result

    formatted = await asyncio.to_thread(
        _encode_payload_to_toon_sync,
        payload,
        settings=settings,
        tool_name=tool_name,
        source=decision.source,
        requested=decision.requested or "toon",
    )
    if setter is not None:
        try:
            setter(formatted)
            return result
        except Exception:
            return formatted
    return formatted


def _apply_resource_output_format(
    payload: Any,
    *,
    settings: Settings,
    resource_name: str,
    format_value: Any,
) -> Any:
    decision = _resolve_output_format(format_value, settings)
    if decision.resolved != "toon":
        return payload
    return _encode_payload_to_toon_sync(
        payload,
        settings=settings,
        tool_name=resource_name,
        source=decision.source,
        requested=decision.requested or "toon",
    )


def _extract_format_param(params: dict[str, Any]) -> Optional[str]:
    raw = params.get("format")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return cast(Optional[str], raw)


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
        # Close repo before returning None to avoid file handle leak
        with suppress(Exception):
            repo.close()
        return None
    with suppress(Exception):
        workspace.resolve().relative_to(root.resolve())
        return repo
    # Close repo before returning None to avoid file handle leak
    with suppress(Exception):
        repo.close()
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


def _validate_iso_timestamp(raw_value: Optional[str], param_name: str = "timestamp") -> Optional[datetime]:
    """Parse and validate an ISO-8601 timestamp, raising helpful error on failure.

    Unlike _parse_iso which silently returns None on failure, this function
    raises a descriptive ToolExecutionError to help agents understand what
    format is expected.

    Parameters
    ----------
    raw_value : Optional[str]
        The timestamp string to parse.
    param_name : str
        The parameter name to include in error messages.

    Returns
    -------
    Optional[datetime]
        Parsed datetime, or None if raw_value was None/empty.

    Raises
    ------
    ToolExecutionError
        If the value is provided but cannot be parsed as ISO-8601.
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
        raise ToolExecutionError(
            error_type="INVALID_TIMESTAMP",
            message=(
                f"Invalid {param_name} format: '{raw_value}'. "
                f"Expected ISO-8601 format like '2025-01-15T10:30:00+00:00' or '2025-01-15T10:30:00Z'. "
                f"Common mistakes: missing timezone (add +00:00 or Z), using slashes instead of dashes, "
                f"or using 12-hour format without AM/PM."
            ),
            recoverable=True,
            data={"provided": raw_value, "expected_format": "YYYY-MM-DDTHH:MM:SS+HH:MM"},
        ) from None


def _validate_program_model(program: str, model: str) -> None:
    """Validate that program and model are non-empty strings.

    Raises
    ------
    ToolExecutionError
        If program or model is empty or whitespace-only.
    """
    if not program or not program.strip():
        raise ToolExecutionError(
            error_type="EMPTY_PROGRAM",
            message=(
                "program cannot be empty. Provide the name of your AI coding tool "
                "(e.g., 'claude-code', 'codex-cli', 'cursor', 'cline')."
            ),
            recoverable=True,
            data={"provided": program},
        )
    if not model or not model.strip():
        raise ToolExecutionError(
            error_type="EMPTY_MODEL",
            message=(
                "model cannot be empty. Provide the underlying model identifier "
                "(e.g., 'claude-opus-4.5', 'gpt-4-turbo', 'claude-sonnet-4')."
            ),
            recoverable=True,
            data={"provided": model},
        )


def _validate_thread_id(raw_value: Optional[str]) -> Optional[str]:
    """Normalize and validate a thread_id used for DB indexing and thread digests."""
    if raw_value is None:
        return None
    thread = raw_value.strip()
    if not thread:
        return None
    if not validate_thread_id_format(thread):
        raise ToolExecutionError(
            error_type="INVALID_THREAD_ID",
            message=(
                f"Invalid thread_id: '{raw_value}'. Thread IDs must start with an alphanumeric character and "
                "contain only letters, numbers, '.', '_', or '-' (max 128). "
                "Examples: 'TKT-123', 'bd-42', 'feature-xyz'."
            ),
            recoverable=True,
            data={"provided": raw_value, "examples": ["TKT-123", "bd-42", "feature-xyz"]},
        )
    return thread


# Patterns that are unsearchable in FTS5 - return None to signal "no results"
_FTS5_UNSEARCHABLE_PATTERNS = frozenset({"*", "**", "***", ".", "..", "...", "?", "??", "???", ""})
_LIKE_FALLBACK_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")
_LIKE_FALLBACK_STOPWORDS = frozenset({"AND", "OR", "NOT", "NEAR"})

# Regex to detect hyphenated tokens that need quoting for FTS5
# Matches: POL-358, FEAT-123, foo-bar-baz, A-1
# Does not match: already-in-quotes, has spaces, etc.
_FTS5_HYPHENATED_TOKEN_RE = re.compile(r"(?<!\")([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)(?!\")")


def _quote_hyphenated_tokens(query: str) -> str:
    """Quote hyphenated tokens in an FTS5 query to treat hyphens as literals.

    FTS5 interprets hyphens as syntax operators. This function detects
    hyphenated tokens (like POL-358, FEAT-123) that are not already quoted
    and wraps them in double quotes for literal matching.

    Parameters
    ----------
    query : str
        The FTS5 query string.

    Returns
    -------
    str
        The query with hyphenated tokens quoted.

    Examples
    --------
    >>> _quote_hyphenated_tokens("POL-358")
    '"POL-358"'
    >>> _quote_hyphenated_tokens("search for FEAT-123 and bd-42")
    'search for "FEAT-123" and "bd-42"'
    >>> _quote_hyphenated_tokens('"already-quoted"')
    '"already-quoted"'
    """
    if not query or "-" not in query:
        return query

    # Don't modify queries that are entirely within quotes
    if query.startswith('"') and query.endswith('"') and query.count('"') == 2:
        return query

    # Replace unquoted hyphenated tokens with quoted versions
    return _FTS5_HYPHENATED_TOKEN_RE.sub(r'"\1"', query)


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards for literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_like_terms(query: str, *, max_terms: int = 5) -> list[str]:
    """Extract LIKE fallback terms from a raw search query."""
    if not query:
        return []
    terms: list[str] = []
    for token in _LIKE_FALLBACK_TOKEN_RE.findall(query):
        if len(token) < 2:
            continue
        if token.upper() in _LIKE_FALLBACK_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= max_terms:
            break
    return terms


def _sanitize_fts_query(query: str) -> str | None:
    """Sanitize an FTS5 query string, fixing common issues where possible.

    SQLite FTS5 has specific syntax requirements. This function attempts to
    fix common mistakes rather than throwing errors. Returns None when the
    query cannot produce meaningful results (caller should return empty list).

    Fixes applied:
    - Strips whitespace
    - Removes leading bare `*` (keeps `term*` prefix patterns)
    - Converts unsearchable patterns to None (empty results)
    - Quotes hyphenated tokens (e.g., POL-358 → "POL-358") to prevent FTS5
      from interpreting the hyphen as a syntax operator

    Parameters
    ----------
    query : str
        The FTS5 query string to sanitize.

    Returns
    -------
    str | None
        The sanitized query string, or None if the query cannot produce results.
        When None is returned, the caller should return an empty result list
        instead of executing the query.
    """
    if not query:
        return None

    trimmed = query.strip()

    if not trimmed:
        return None

    # Check for bare patterns that can't match anything meaningful in FTS5
    if trimmed in _FTS5_UNSEARCHABLE_PATTERNS:
        return None

    # Bare boolean operators without terms - can't search
    upper_trimmed = trimmed.upper()
    if upper_trimmed in {"AND", "OR", "NOT"}:
        return None

    # FTS5 doesn't support leading wildcards (*foo), only trailing (foo*).
    # Strip leading "*" regardless of what follows: "*foo" -> "foo", "* bar" -> "bar"
    if trimmed.startswith("*"):
        if len(trimmed) == 1:
            return None
        # Strip leading "*" (and any following whitespace) and recurse
        return _sanitize_fts_query(trimmed[1:].lstrip())

    # Fix trailing lone asterisks that aren't part of prefix patterns
    # e.g., "foo *" -> "foo"
    if trimmed.endswith(" *"):
        trimmed = trimmed[:-2].rstrip()
        if not trimmed:
            return None

    # Multiple consecutive spaces -> single space
    trimmed = re.sub(r" {2,}", " ", trimmed)

    # Quote hyphenated tokens to prevent FTS5 from interpreting hyphens as operators
    # e.g., "POL-358" would otherwise fail with "no such column: 358"
    trimmed = _quote_hyphenated_tokens(trimmed)

    return trimmed if trimmed else None


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

def _compute_project_slug(human_key: str) -> str:
    """
    Compute the project slug with strict backward compatibility by default.
    When worktree-friendly behavior is enabled, we still default to 'dir' mode
    until additional identity modes are implemented.
    """
    settings = get_settings()
    # Gate: preserve existing behavior unless explicitly enabled
    if not settings.worktrees_enabled:
        return slugify(human_key)
    # Helpers for identity modes (privacy-safe)
    def _short_sha1(text: str, n: int = 10) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]

    def _norm_remote(url: str | None) -> str | None:
        if not url:
            return None
        url = url.strip()
        try:
            if url.startswith("git@"):
                host = url.split("@", 1)[1].split(":", 1)[0]
                path = url.split(":", 1)[1]
            else:
                from urllib.parse import urlparse as _urlparse

                p = _urlparse(url)
                host = p.hostname or ""
                path = (p.path or "")
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

    mode = (settings.project_identity_mode or "dir").strip().lower()
    # Mode: git-remote
    if mode == "git-remote":
        try:
            # Attempt to use GitPython for robustness across worktrees
            with _git_repo(human_key) as repo:
                remote_name = settings.project_identity_remote or "origin"
                remote_url: str | None = None
                # Prefer 'git remote get-url' to support multiple urls/rewrite rules
                try:
                    remote_url = repo.git.remote("get-url", remote_name).strip() or None
                except Exception:
                    # Fallback: use config if available
                    try:
                        remote = next((r for r in repo.remotes if r.name == remote_name), None)
                        if remote and remote.urls:
                            remote_url = next(iter(remote.urls), None)
                    except Exception:
                        remote_url = None
                normalized = _norm_remote(remote_url)
                if normalized:
                    base = normalized.rsplit("/", 1)[-1] or "repo"
                    canonical = normalized  # privacy-safe canonical string
                    return f"{base}-{_short_sha1(canonical)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            # Non-git directory or error; fall through to fallback
            pass
        # Fallback to dir behavior if we cannot resolve a normalized remote
        return slugify(human_key)

    # Mode: git-toplevel
    if mode == "git-toplevel":
        try:
            with _git_repo(human_key) as repo:
                top = repo.git.rev_parse("--show-toplevel").strip()
                if top:
                    from pathlib import Path as _P

                    top_real = str(_P(top).resolve())
                    base = _P(top_real).name or "repo"
                    return f"{base}-{_short_sha1(top_real)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            return slugify(human_key)
        return slugify(human_key)

    # Mode: git-common-dir
    if mode == "git-common-dir":
        try:
            with _git_repo(human_key) as repo:
                # Prefer GitPython's common_dir which normalizes worktree paths
                try:
                    gdir = getattr(repo, "common_dir", None)
                except Exception:
                    gdir = None
                if not gdir:
                    gdir = repo.git.rev_parse("--git-common-dir").strip()
                if gdir:
                    from pathlib import Path as _P

                    gdir_real = str(_P(gdir).resolve())
                    base = "repo"
                    return f"{base}-{_short_sha1(gdir_real)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            return slugify(human_key)
        return slugify(human_key)

    # Default and 'dir' mode: strict back-compat
    return slugify(human_key)


def _resolve_project_identity(human_key: str) -> dict[str, Any]:
    """
    Resolve identity details for a given human_key path.
    Returns: { slug, identity_mode_used, canonical_path, human_key,
               repo_root, git_common_dir, branch, worktree_name,
               core_ignorecase, normalized_remote, project_uid }
    Writes a private marker under .git/agent-mail/project-id when WORKTREES_ENABLED=1
    and no marker exists yet.
    """
    settings_local = get_settings()
    mode_config = (settings_local.project_identity_mode or "dir").strip().lower()
    mode_used = "dir" if not settings_local.worktrees_enabled else mode_config
    target_path = str(Path(human_key).expanduser().resolve())

    if not settings_local.worktrees_enabled:
        # Keep default behavior lightweight when worktree features are disabled.
        # (Avoid touching GitPython / spawning git subprocesses unnecessarily.)
        slug_value = slugify(human_key)
        try:
            project_uid = hashlib.sha1(target_path.encode("utf-8")).hexdigest()[:20]
        except Exception:
            project_uid = str(uuid.uuid4())
        return {
            "slug": slug_value,
            "identity_mode_used": "dir",
            "canonical_path": target_path,
            "human_key": human_key,
            "repo_root": None,
            "git_common_dir": None,
            "branch": None,
            "worktree_name": None,
            "core_ignorecase": None,
            "normalized_remote": None,
            "project_uid": project_uid,
            "discovery": None,
        }

    repo_root: Optional[str] = None
    git_common_dir: Optional[str] = None
    branch: Optional[str] = None
    default_branch: Optional[str] = None
    worktree_name: Optional[str] = None
    core_ignorecase: Optional[bool] = None
    normalized_remote: Optional[str] = None
    canonical_path: str = target_path

    def _norm_remote(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        u = url.strip()
        try:
            host = ""
            path = ""
            # SCP-like: git@host:owner/repo.git
            if "@" in u and ":" in u and not u.startswith(("http://", "https://", "ssh://", "git://")):
                at_pos = u.find("@")
                colon_pos = u.find(":", at_pos + 1)
                if colon_pos != -1:
                    host = u[at_pos + 1 : colon_pos]
                    path = u[colon_pos + 1 :]
            else:
                from urllib.parse import urlparse as _urlparse
                pr = _urlparse(u)
                host = (pr.hostname or "").lower()
                # Some ssh URLs include port; ignore
                path = (pr.path or "")
            host = host.lower()
            if not host:
                return None
            path = path.lstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            # collapse duplicate slashes
            while "//" in path:
                path = path.replace("//", "/")
            parts = [seg for seg in path.split("/") if seg]
            if len(parts) < 2:
                return None
            # Keep the last two segments (owner/repo) and normalize to lowercase
            # This supports nested group paths (e.g., GitLab subgroups)
            if len(parts) >= 2:
                owner, repo_name = parts[-2].lower(), parts[-1].lower()
            else:
                return None
            return f"{host}/{owner}/{repo_name}"
        except Exception:
            return None

    # Discovery YAML: optional override
    def _read_discovery_yaml(base_dir: str) -> dict[str, Any]:
        try:
            ypath = Path(base_dir) / ".agent-mail.yaml"
            if not ypath.exists():
                return {}
            # Prefer PyYAML when available for robust parsing; fallback to minimal parser
            try:
                import yaml as _yaml
                loaded = _yaml.safe_load(ypath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    # Keep only known keys to avoid surprises
                    allowed = {"project_uid", "product_uid"}
                    return {k: str(v) for k, v in loaded.items() if k in allowed and isinstance(v, (str, int))}
                return {}
            except Exception:
                data = {}
                for line in ypath.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or ":" not in s:
                        continue
                    key, value = s.split(":", 1)
                    k = key.strip()
                    if k not in {"project_uid", "product_uid"}:
                        continue
                    # strip inline comments
                    v = value.split("#", 1)[0].strip().strip("'\"")
                    if v:
                        data[k] = v
                return data
        except Exception:
            return {}

    try:
        with _git_repo(target_path) as repo:
            repo_root = str(Path(repo.working_tree_dir or "").resolve())
            try:
                git_common_dir = repo.git.rev_parse("--git-common-dir").strip()
            except Exception:
                git_common_dir = None
            try:
                branch = repo.active_branch.name
            except Exception:
                try:
                    branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
                except Exception:
                    branch = None
            try:
                worktree_name = Path(repo.working_tree_dir or "").name or None
            except Exception:
                worktree_name = None
            try:
                core_ic = repo.config_reader().get_value("core", "ignorecase", "false")
                core_ignorecase = str(core_ic).strip().lower() == "true"
            except Exception:
                core_ignorecase = None
            remote_name = settings_local.project_identity_remote or "origin"
            remote_url_local: Optional[str] = None
            try:
                remote_url_local = repo.git.remote("get-url", remote_name).strip() or None
            except Exception:
                try:
                    r = next((r for r in repo.remotes if r.name == remote_name), None)
                    if r and r.urls:
                        remote_url_local = next(iter(r.urls), None)
                except Exception:
                    remote_url_local = None
            normalized_remote = _norm_remote(remote_url_local)
            try:
                sym = repo.git.symbolic_ref(
                    f"refs/remotes/{settings_local.project_identity_remote or 'origin'}/HEAD"
                ).strip()
                if sym.startswith("refs/remotes/"):
                    default_branch = sym.rsplit("/", 1)[-1]
            except Exception:
                default_branch = "main"
    except (InvalidGitRepositoryError, NoSuchPathError, Exception):
        pass  # Non-git directory; continue with fallback values

    if mode_used == "git-remote" and normalized_remote:
        canonical_path = normalized_remote
    elif mode_used == "git-toplevel" and repo_root:
        canonical_path = repo_root
    elif mode_used == "git-common-dir" and git_common_dir:
        canonical_path = str(Path(git_common_dir).resolve())
    else:
        canonical_path = target_path

    # Compute project_uid via precedence:
    # committed marker -> discovery yaml -> private marker -> remote fingerprint -> git-common-dir hash -> dir hash
    marker_committed: Optional[Path] = Path(repo_root or "") / ".agent-mail-project-id" if repo_root else None
    marker_private: Optional[Path] = Path(git_common_dir or "") / "agent-mail" / "project-id" if git_common_dir else None
    # Normalize marker_private to absolute if git_common_dir is relative (common for non-linked worktrees)
    if marker_private is not None and not marker_private.is_absolute():
        try:
            base = Path(repo_root or target_path)
            marker_private = (base / marker_private).resolve()
        except Exception:
            pass
    discovery: dict[str, Any] = _read_discovery_yaml(repo_root or target_path)
    project_uid: Optional[str] = None
    try:
        if marker_committed and marker_committed.exists():
            project_uid = (marker_committed.read_text(encoding="utf-8").strip() or None)
    except Exception:
        project_uid = None
    if not project_uid:
        # Discovery yaml override
        uid = str(discovery.get("project_uid", "")).strip() if discovery else ""
        if uid:
            project_uid = uid
    if not project_uid:
        try:
            if marker_private and marker_private.exists():
                project_uid = (marker_private.read_text(encoding="utf-8").strip() or None)
        except Exception:
            project_uid = None
    if not project_uid:
        # Remote fingerprint
        remote_uid: Optional[str] = None
        try:
            if normalized_remote:
                fingerprint = f"{normalized_remote}@{default_branch or 'main'}"
                remote_uid = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]
        except Exception:
            remote_uid = None
        if remote_uid:
            project_uid = remote_uid
    if not project_uid and git_common_dir:
        try:
            project_uid = hashlib.sha1(str(Path(git_common_dir).resolve()).encode("utf-8")).hexdigest()[:20]
        except Exception:
            project_uid = None
    if not project_uid:
        try:
            project_uid = hashlib.sha1(target_path.encode("utf-8")).hexdigest()[:20]
        except Exception:
            project_uid = str(uuid.uuid4())

    # Write private marker if gated and we have a git common dir
    if settings_local.worktrees_enabled and marker_private and not marker_private.exists():
        try:
            marker_private.parent.mkdir(parents=True, exist_ok=True)
            marker_private.write_text(project_uid + "\n", encoding="utf-8")
        except Exception:
            pass

    slug_value = _compute_project_slug(target_path)
    payload = {
        "slug": slug_value,
        "identity_mode_used": mode_used,
        "canonical_path": canonical_path,
        "human_key": target_path,
        "repo_root": repo_root,
        "git_common_dir": git_common_dir,
        "branch": branch,
        "worktree_name": worktree_name,
        "core_ignorecase": core_ignorecase,
        "normalized_remote": normalized_remote,
        "project_uid": project_uid,
        "discovery": discovery or None,
    }
    # Rich-styled identity decision logging (optional)
    try:
        if get_settings().tools_log_enabled:
            from rich.console import Console as _Console  # local import to avoid global dependency
            from rich.table import Table as _Table
            console = _Console()
            table = _Table(title="Identity Resolution", show_header=True, header_style="bold white on blue")
            table.add_column("Field", style="bold cyan")
            table.add_column("Value")
            table.add_row("Mode", str(payload["identity_mode_used"] or "dir"))
            table.add_row("Slug", str(payload["slug"]))
            table.add_row("Canonical", str(payload["canonical_path"]))
            table.add_row("Repo Root", str(payload["repo_root"] or ""))
            table.add_row("Git Common Dir", str(payload["git_common_dir"] or ""))
            table.add_row("Branch", str(payload["branch"] or ""))
            table.add_row("Worktree", str(payload["worktree_name"] or ""))
            table.add_row("Ignorecase", str(payload["core_ignorecase"]))
            table.add_row("Normalized Remote", str(payload["normalized_remote"] or ""))
            table.add_row("Project UID", str(payload["project_uid"] or ""))
            console.print(table)
    except Exception:
        # Never fail due to logging
        pass
    return payload

async def _ensure_project(human_key: str) -> Project:
    await ensure_schema()
    # Resolve symlinks to canonical path so /dp/ntm and /data/projects/ntm
    # resolve to the same project identity
    human_key = str(Path(human_key).resolve())
    slug = _compute_project_slug(human_key)
    for attempt in range(6):
        try:
            async with get_session() as session:
                result = await session.execute(select(Project).where(Project.slug == slug))
                project = result.scalars().first()
                if project:
                    return project
                project = Project(slug=slug, human_key=human_key)
                session.add(project)
                try:
                    await session.commit()
                except IntegrityError:
                    # Concurrent ensure_project: another caller created the row. Treat as idempotent.
                    await session.rollback()
                    result = await session.execute(select(Project).where(Project.slug == slug))
                    project = result.scalars().first()
                    if project:
                        return project
                    raise
                await session.refresh(project)
                return project
        except OperationalError as exc:
            error_msg = str(exc).lower()
            is_lock_error = any(phrase in error_msg for phrase in ("database is locked", "database is busy", "locked"))
            if not is_lock_error or attempt >= 5:
                raise
            await asyncio.sleep(min(0.05 * (2**attempt), 0.5))

    raise RuntimeError("ensure_project retry loop exited unexpectedly")

    # -- Identity inspection resource is registered inside build_mcp_server below


# --- Smart lookup helpers with fuzzy matching and suggestions -----------------------------------


def _similarity_score(a: str, b: str) -> float:
    """Compute similarity score between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def _find_similar_projects(identifier: str, limit: int = 5, min_score: float = 0.4) -> list[tuple[str, str, float]]:
    """Find projects with similar slugs/names. Returns list of (slug, human_key, score)."""
    slug = slugify(identifier)
    suggestions: list[tuple[str, str, float]] = []
    async with get_session() as session:
        result = await session.execute(select(Project))
        projects = result.scalars().all()
        for p in projects:
            # Check both slug and human_key similarity
            slug_score = _similarity_score(slug, p.slug)
            key_score = _similarity_score(identifier, p.human_key) if p.human_key else 0.0
            best_score = max(slug_score, key_score)
            if best_score >= min_score:
                suggestions.append((p.slug, p.human_key, best_score))
    suggestions.sort(key=lambda x: x[2], reverse=True)
    return suggestions[:limit]


async def _find_similar_agents(project: Project, name: str, limit: int = 5, min_score: float = 0.4) -> list[tuple[str, float]]:
    """Find agents with similar names in the project. Returns list of (name, score)."""
    suggestions: list[tuple[str, float]] = []
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(cast(Any, Agent.project_id == project.id))
        )
        agents = result.scalars().all()
        for a in agents:
            score = _similarity_score(name, a.name)
            if score >= min_score:
                suggestions.append((a.name, score))
    suggestions.sort(key=lambda x: x[1], reverse=True)
    return suggestions[:limit]


async def _list_project_agents(project: Project, limit: int = 10) -> list[str]:
    """List agent names in a project."""
    async with get_session() as session:
        result = await session.execute(
            select(Agent.name).where(cast(Any, Agent.project_id == project.id)).limit(limit)
        )
        return [row[0] for row in result.all()]


async def _get_project_by_identifier(identifier: str) -> Project:
    """Get project by identifier with helpful error messages and suggestions."""
    await ensure_schema()

    # Validate input
    if not identifier or not identifier.strip():
        raise ToolExecutionError(
            "INVALID_ARGUMENT",
            "Project identifier cannot be empty. Provide a project path like '/data/projects/myproject' or a slug like 'myproject'.",
            recoverable=True,
            data={"parameter": "project_key", "provided": repr(identifier)},
        )

    raw_identifier = identifier.strip()
    canonical_identifier = raw_identifier
    # Resolve absolute paths to canonical form so symlink aliases map to one project.
    try:
        candidate = Path(raw_identifier).expanduser()
        if candidate.is_absolute():
            canonical_identifier = str(candidate.resolve())
    except Exception:
        canonical_identifier = raw_identifier

    # Detect common placeholder patterns - these indicate unconfigured hooks/settings
    _placeholder_patterns = [
        "YOUR_PROJECT",
        "YOUR_PROJECT_PATH",
        "YOUR_PROJECT_KEY",
        "PLACEHOLDER",
        "<PROJECT>",
        "{PROJECT}",
        "$PROJECT",
    ]
    identifier_upper = raw_identifier.upper()
    for pattern in _placeholder_patterns:
        if pattern in identifier_upper or identifier_upper == pattern:
            raise ToolExecutionError(
                "CONFIGURATION_ERROR",
                f"Detected placeholder value '{identifier}' instead of a real project path. "
                f"This typically means a hook or integration script hasn't been configured yet. "
                f"Replace placeholder values in your .claude/settings.json or environment variables "
                f"with actual project paths like '/Users/you/projects/myproject'.",
                recoverable=True,
                data={
                    "parameter": "project_key",
                    "provided": identifier,
                    "detected_placeholder": pattern,
                    "fix_hint": "Update AGENT_MAIL_PROJECT or project_key in your configuration",
                },
            )

    slug = slugify(canonical_identifier)
    async with get_session() as session:
        result = await session.execute(
            select(Project).where(
                or_(
                    Project.slug == slug,
                    Project.human_key == canonical_identifier,
                    Project.human_key == raw_identifier,
                )
            )
        )
        project = result.scalars().first()
        if project:
            return project

    # Project not found - provide helpful suggestions
    suggestions = await _find_similar_projects(raw_identifier)

    if suggestions:
        suggestion_text = ", ".join([f"'{s[0]}'" for s in suggestions[:3]])
        raise ToolExecutionError(
            "NOT_FOUND",
            f"Project '{raw_identifier}' not found. Did you mean: {suggestion_text}? "
            f"Use ensure_project to create a new project, or check spelling.",
            recoverable=True,
            data={
                "identifier": raw_identifier,
                "slug_searched": slug,
                "suggestions": [{"slug": s[0], "human_key": s[1], "score": round(s[2], 2)} for s in suggestions],
            },
        )
    else:
        raise ToolExecutionError(
            "NOT_FOUND",
            f"Project '{raw_identifier}' not found and no similar projects exist. "
            f"Use ensure_project to create a new project first. "
            f"Example: ensure_project(human_key='/path/to/your/project')",
            recoverable=True,
            data={"identifier": raw_identifier, "slug_searched": slug},
        )


# --- Common mistake detection helpers --------------------------------------------------------

# Known program names that agents might mistakenly use as agent names
_KNOWN_PROGRAM_NAMES: frozenset[str] = frozenset({
    "claude-code", "claude", "codex-cli", "codex", "cursor", "windsurf",
    "cline", "aider", "copilot", "github-copilot", "gemini-cli", "gemini",
    "opencode", "vscode", "neovim", "vim", "emacs", "zed", "continue",
})

# Known model name patterns that agents might mistakenly use as agent names
_MODEL_NAME_PATTERNS: tuple[str, ...] = (
    "gpt-", "gpt4", "gpt3", "claude-", "opus", "sonnet", "haiku",
    "gemini-", "llama", "mistral", "codestral", "o1-", "o3-",
)


def _looks_like_program_name(value: str) -> bool:
    """Check if value looks like a program name (not a valid agent name)."""
    v = value.lower().strip()
    return v in _KNOWN_PROGRAM_NAMES


def _looks_like_model_name(value: str) -> bool:
    """Check if value looks like a model name (not a valid agent name)."""
    v = value.lower().strip()
    return any(p in v for p in _MODEL_NAME_PATTERNS)


def _looks_like_email(value: str) -> bool:
    """Check if value looks like an email address."""
    return "@" in value and "." in value.split("@")[-1]


def _looks_like_broadcast(value: str) -> bool:
    """Check if value looks like a broadcast attempt."""
    v = value.lower().strip()
    return v in {"all", "*", "everyone", "broadcast", "@all", "@everyone"}


def _looks_like_descriptive_name(value: str) -> bool:
    """Check if value looks like a descriptive role name instead of adjective+noun."""
    v = value.lower()
    # Common suffixes for descriptive agent names
    descriptive_patterns = (
        "agent", "bot", "assistant", "helper", "manager", "coordinator",
        "developer", "engineer", "migrator", "refactorer", "fixer",
        "harmonizer", "integrator", "optimizer", "analyzer", "worker",
    )
    return any(v.endswith(p) for p in descriptive_patterns)


def _looks_like_unix_username(value: str) -> bool:
    """
    Check if value looks like a Unix username rather than an adjective+noun agent name.

    This helps detect when hooks or scripts pass $USER instead of the actual agent name.
    Unix usernames typically:
    - Are all lowercase
    - Don't contain capital letters (unlike CamelCase agent names)
    - Are short (3-12 chars typically)
    - Often match common first name patterns
    """
    v = value.strip()
    if not v:
        return False

    # Agent names are PascalCase (e.g., "GreenLake"), usernames are usually all lowercase
    # If there are no uppercase letters and it's a single "word", it's likely a username
    if v.islower() and v.isalnum() and 2 <= len(v) <= 16:
        # Additional check: if it doesn't match any adjective or noun, more likely a username
        from mcp_agent_mail.utils import ADJECTIVES, NOUNS
        if v.lower() not in {a.lower() for a in ADJECTIVES} and v.lower() not in {n.lower() for n in NOUNS}:
            return True

    return False


def _detect_agent_name_mistake(value: str) -> tuple[str, str] | None:
    """
    Detect common mistakes when agents provide invalid agent names.
    Returns (mistake_type, helpful_message) or None if no obvious mistake detected.
    """
    if _looks_like_program_name(value):
        return (
            "PROGRAM_NAME_AS_AGENT",
            f"'{value}' looks like a program name, not an agent name. "
            f"Agent names must be adjective+noun combinations like 'BlueLake' or 'GreenCastle'. "
            f"Use the 'program' parameter for program names, and omit 'name' to auto-generate a valid agent name."
        )
    if _looks_like_model_name(value):
        return (
            "MODEL_NAME_AS_AGENT",
            f"'{value}' looks like a model name, not an agent name. "
            f"Agent names must be adjective+noun combinations like 'RedStone' or 'PurpleBear'. "
            f"Use the 'model' parameter for model names, and omit 'name' to auto-generate a valid agent name."
        )
    if _looks_like_email(value):
        return (
            "EMAIL_AS_AGENT",
            f"'{value}' looks like an email address. Agent names are simple identifiers like 'BlueDog', "
            f"not email addresses. Check the 'to' parameter format."
        )
    if _looks_like_broadcast(value):
        return (
            "BROADCAST_ATTEMPT",
            f"'{value}' looks like a broadcast attempt. Agent Mail doesn't support broadcasting to all agents. "
            f"List specific recipient agent names in the 'to' parameter."
        )
    if _looks_like_descriptive_name(value):
        return (
            "DESCRIPTIVE_NAME",
            f"'{value}' looks like a descriptive role name. Agent names must be randomly generated "
            f"adjective+noun combinations like 'WhiteMountain' or 'BrownCreek', NOT descriptive of the agent's task. "
            f"Omit the 'name' parameter to auto-generate a valid name."
        )
    if _looks_like_unix_username(value):
        return (
            "UNIX_USERNAME_AS_AGENT",
            f"'{value}' looks like a Unix username (possibly from $USER environment variable). "
            f"Agent names must be adjective+noun combinations like 'BlueLake' or 'GreenCastle'. "
            f"When you called register_agent, the system likely auto-generated a valid name for you. "
            f"To find your actual agent name, check the response from register_agent or use "
            f"resource://agents/{{project_key}} to list all registered agents in this project."
        )
    return None


def _detect_suspicious_file_reservation(pattern: str) -> str | None:
    """
    Detect suspicious file reservation patterns that might be too broad.
    Returns a warning message or None if the pattern looks reasonable.
    """
    p = pattern.strip()

    # Catch overly broad patterns
    if p in ("*", "**", "**/*", "**/**", "."):
        return (
            f"Pattern '{p}' is too broad and would reserve the entire project. "
            f"Use more specific patterns like 'src/api/*.py' or 'lib/auth/**'."
        )

    # Catch absolute paths when relative expected
    if p.startswith("/") and not p.startswith("//"):
        return (
            f"Pattern '{p}' looks like an absolute path. File reservation patterns should be "
            f"project-relative (e.g., 'src/module.py' not '/full/path/src/module.py')."
        )

    # Warn about very short patterns that might be unintentionally broad
    if len(p) <= 2 and "*" in p:
        return (
            f"Pattern '{p}' is very short and may match more files than intended. "
            f"Consider using a more specific pattern."
        )

    return None


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
async def _archive_write_lock(archive: ProjectArchive, *, timeout_seconds: float = 60.0) -> AsyncIterator[None]:
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
        naive_now = _naive_utc(now)
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
            record.evaluated_ts = naive_now
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
                elif entry["status"] != "dismissed" and float(cast(float, entry_base["score"])) >= _PROJECT_SIBLING_MIN_SUGGESTION_SCORE:
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
                    or_(Agent.project_id == pair[0], cast(Any, Agent.project_id) == pair[1])
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
        naive_now = _naive_utc(now)
        suggestion.status = normalized_status
        suggestion.evaluated_ts = naive_now
        if normalized_status == "confirmed":
            suggestion.confirmed_ts = naive_now
            suggestion.dismissed_ts = None
        elif normalized_status == "dismissed":
            suggestion.dismissed_ts = naive_now
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
            # When coercing, if the provided hint is not in the valid adjective+noun set,
            # silently fall back to auto-generation instead of erroring.
            if validate_agent_name_format(sanitized):
                if not await available(sanitized):
                    # In strict mode, indicate conflict; in coerce, fall back to generation
                    if mode == "strict":
                        raise ValueError(f"Agent name '{sanitized}' is already in use.")
                else:
                    return sanitized
            else:
                if mode == "strict":
                    raise ValueError(
                        f"Invalid agent name format: '{sanitized}'. "
                        f"Agent names MUST be randomly generated adjective+noun combinations "
                        f"(e.g., 'GreenLake', 'BlueDog'), NOT descriptive names. "
                        f"Omit the 'name_hint' parameter to auto-generate a valid name."
                    )
        else:
            # No alphanumerics remain; only strict mode should error
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
    explicit_name_used = False
    if mode == "always_auto" or name is None:
        desired_name = await _generate_unique_agent_name(project, settings, None)
    else:
        sanitized = sanitize_agent_name(name)
        if not sanitized:
            if mode == "strict":
                raise ValueError("Agent name must contain alphanumeric characters.")
            desired_name = await _generate_unique_agent_name(project, settings, None)
        else:
            if validate_agent_name_format(sanitized):
                desired_name = sanitized
                explicit_name_used = True
            else:
                if mode == "strict":
                    # Check for common mistakes and provide specific guidance
                    mistake = _detect_agent_name_mistake(sanitized)
                    if mistake:
                        raise ToolExecutionError(
                            mistake[0],
                            mistake[1],
                            recoverable=True,
                            data={"provided_name": sanitized, "valid_examples": ["BlueLake", "GreenCastle", "RedStone"]},
                        )
                    raise ToolExecutionError(
                        "INVALID_AGENT_NAME",
                        f"Invalid agent name format: '{sanitized}'. "
                        f"Agent names MUST be randomly generated adjective+noun combinations "
                        f"(e.g., 'GreenLake', 'BlueDog'), NOT descriptive names. "
                        f"Omit the 'name' parameter to auto-generate a valid name.",
                        recoverable=True,
                        data={"provided_name": sanitized, "valid_examples": ["BlueLake", "GreenCastle", "RedStone"]},
                    )
                # coerce -> ignore invalid provided name and auto-generate
                desired_name = await _generate_unique_agent_name(project, settings, None)
    await ensure_schema()
    async with get_session() as session:
        for _attempt in range(5):
            # Use case-insensitive matching to be consistent with _agent_name_exists() and _get_agent()
            result = await session.execute(
                select(Agent).where(
                    cast(Any, Agent.project_id == project.id),
                    cast(Any, func.lower(Agent.name) == desired_name.lower()),
                )
            )
            agent = result.scalars().first()
            if agent:
                agent.program = program
                agent.model = model
                agent.task_description = task_description
                agent.last_active_ts = _naive_utc()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                break

            candidate = Agent(
                project_id=project.id,
                name=desired_name,
                program=program,
                model=model,
                task_description=task_description,
            )
            session.add(candidate)
            try:
                await session.commit()
                await session.refresh(candidate)
                agent = candidate
                break
            except IntegrityError:
                await session.rollback()
                with suppress(Exception):
                    session.expunge(candidate)

                if explicit_name_used:
                    # Another concurrent call created this identity; treat as idempotent update.
                    result = await session.execute(
                        select(Agent).where(
                            cast(Any, Agent.project_id == project.id),
                            cast(Any, func.lower(Agent.name) == desired_name.lower()),
                        )
                    )
                    agent = result.scalars().first()
                    if agent is None:
                        raise
                    agent.program = program
                    agent.model = model
                    agent.task_description = task_description
                    agent.last_active_ts = _naive_utc()
                    session.add(agent)
                    await session.commit()
                    await session.refresh(agent)
                    break

                # Auto-generated name collision under concurrency: pick a new name and retry.
                desired_name = await _generate_unique_agent_name(project, settings, None)
                continue
        else:
            raise RuntimeError("Failed to create a unique agent after multiple retries.")
    archive = await ensure_archive(settings, project.slug)
    async with _archive_write_lock(archive):
        await write_agent_profile(archive, _agent_to_dict(agent))
    return agent


async def _get_agent(project: Project, name: str) -> Agent:
    """Get agent by name with helpful error messages and suggestions."""
    await ensure_schema()

    # Validate input
    if not name or not name.strip():
        raise ToolExecutionError(
            "INVALID_ARGUMENT",
            f"Agent name cannot be empty. Provide a valid agent name for project '{project.human_key}'.",
            recoverable=True,
            data={"parameter": "agent_name", "provided": repr(name), "project": project.slug},
        )

    # Detect placeholder values (indicates unconfigured hooks/settings)
    _agent_placeholder_patterns = [
        "YOUR_AGENT",
        "YOUR_AGENT_NAME",
        "AGENT_NAME",
        "PLACEHOLDER",
        "<AGENT>",
        "{AGENT}",
        "$AGENT",
    ]
    name_upper = name.upper().strip()
    for pattern in _agent_placeholder_patterns:
        if pattern in name_upper or name_upper == pattern:
            raise ToolExecutionError(
                "CONFIGURATION_ERROR",
                f"Detected placeholder value '{name}' instead of a real agent name. "
                f"This typically means a hook or integration script hasn't been configured yet. "
                f"Replace placeholder values with your actual agent name (e.g., 'BlueMountain').",
                recoverable=True,
                data={
                    "parameter": "agent_name",
                    "provided": name,
                    "detected_placeholder": pattern,
                    "fix_hint": "Update AGENT_MAIL_AGENT or agent_name in your configuration",
                },
            )

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name) == name.lower())
        )
        agent = result.scalars().first()
        if agent:
            return agent

    # Agent not found - provide helpful suggestions
    suggestions = await _find_similar_agents(project, name)
    available_agents = await _list_project_agents(project)

    # Check for common mistakes (Unix username, program name, etc.)
    mistake = _detect_agent_name_mistake(name)
    mistake_hint = ""
    if mistake:
        mistake_hint = f"\n\nHINT: {mistake[1]}"

    if suggestions:
        # Found similar names - probably a typo
        suggestion_text = ", ".join([f"'{s[0]}'" for s in suggestions[:3]])
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found in project '{project.human_key}'. Did you mean: {suggestion_text}? "
            f"Agent names are case-insensitive but must match exactly.{mistake_hint}",
            recoverable=True,
            data={
                "agent_name": name,
                "project": project.slug,
                "suggestions": [{"name": s[0], "score": round(s[1], 2)} for s in suggestions],
                "available_agents": available_agents,
                "mistake_type": mistake[0] if mistake else None,
            },
        )
    elif available_agents:
        # No similar names but project has agents
        agents_list = ", ".join([f"'{a}'" for a in available_agents[:5]])
        more_text = f" and {len(available_agents) - 5} more" if len(available_agents) > 5 else ""
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found in project '{project.human_key}'. "
            f"Available agents: {agents_list}{more_text}. "
            f"Use register_agent to create a new agent identity.{mistake_hint}",
            recoverable=True,
            data={
                "agent_name": name,
                "project": project.slug,
                "available_agents": available_agents,
                "mistake_type": mistake[0] if mistake else None,
            },
        )
    else:
        # Project has no agents
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found. Project '{project.human_key}' has no registered agents yet. "
            f"Use register_agent to create an agent identity first (omit 'name' to auto-generate a valid one). "
            f"Example: register_agent(project_key='{project.slug}', program='claude-code', model='opus-4'){mistake_hint}",
            recoverable=True,
            data={"agent_name": name, "project": project.slug, "available_agents": [], "mistake_type": mistake[0] if mistake else None},
        )


async def _get_agents_batch(project: Project, names: Sequence[str]) -> dict[str, Agent]:
    """Batch lookup agents by name with `_get_agent`-equivalent error reporting."""
    await ensure_schema()
    if not names:
        return {}
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")

    lowered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        lowered_names.append(lowered)

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name).in_(lowered_names))
        )
        agents = result.scalars().all()

    by_lower = {agent.name.lower(): agent for agent in agents}
    resolved: dict[str, Agent] = {}
    missing: list[str] = []
    for name in names:
        agent = by_lower.get(name.lower())
        if agent is None:
            missing.append(name)
        else:
            resolved[name] = agent

    if missing:
        # Reuse the exact error logic from _get_agent for the first missing entry.
        await _get_agent(project, missing[0])

    return resolved


async def _get_agents_batch_lenient(project: Project, names: Sequence[str]) -> dict[str, Agent]:
    """Batch lookup agents by name, silently skipping missing agents.

    Unlike _get_agents_batch, this does NOT raise errors for missing agents.
    Use this for contact policy enforcement where missing recipients should
    be skipped rather than treated as errors.

    Parameters
    ----------
    project : Project
        The project to look up agents in.
    names : Sequence[str]
        Agent names to look up.

    Returns
    -------
    dict[str, Agent]
        Mapping from original name to Agent. Missing agents are omitted.
    """
    await ensure_schema()
    if not names:
        return {}
    if project.id is None:
        return {}

    # Deduplicate and lowercase for efficient IN query
    lowered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        lowered_names.append(lowered)

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.project_id == project.id, func.lower(Agent.name).in_(lowered_names))
        )
        agents = result.scalars().all()

    # Build lookup by lowercase name
    by_lower = {agent.name.lower(): agent for agent in agents}

    # Resolve original names to agents (preserving original case in keys)
    resolved: dict[str, Agent] = {}
    for name in names:
        agent = by_lower.get(name.lower())
        if agent is not None:
            resolved[name] = agent

    return resolved


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
        sender.last_active_ts = _naive_utc()
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
    expires = _naive_utc() + timedelta(seconds=ttl_seconds)
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


def _file_reservation_payload(
    project: Project,
    reservation: FileReservation,
    agent: Agent,
    *,
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
    reason_override: Optional[str] = None,
) -> dict[str, Any]:
    """Build a normalized payload for Git archive file_reservation records.

    If released_ts is set, clamp expires_ts to released_ts so client-side guards
    treat the reservation as inactive even if the original expiry was later.
    """
    released_dt = _ensure_utc(reservation.released_ts)
    expires_dt = _ensure_utc(reservation.expires_ts)
    if released_dt and expires_dt:
        if released_dt < expires_dt:
            expires_dt = released_dt
    elif released_dt and expires_dt is None:
        expires_dt = released_dt

    payload: dict[str, Any] = {
        "id": reservation.id,
        "project": project.human_key,
        "agent": agent.name,
        "path_pattern": reservation.path_pattern,
        "exclusive": reservation.exclusive,
        "reason": reason_override if reason_override is not None else reservation.reason,
        "created_ts": _iso(reservation.created_ts),
        "expires_ts": _iso(expires_dt) if expires_dt else _iso(reservation.expires_ts),
    }
    if released_dt is not None:
        payload["released_ts"] = _iso(released_dt)
    if branch:
        payload["branch"] = branch
    if worktree:
        payload["worktree"] = worktree
    return payload


async def _write_file_reservation_records(
    project: Project,
    records: Sequence[tuple[FileReservation, Agent]],
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
    reason_override: Optional[str] = None,
) -> None:
    if not records:
        return
    if archive_locked and archive is None:
        raise ValueError("archive_locked=True requires a provided archive")
    settings = get_settings()
    target_archive = archive or await ensure_archive(settings, project.slug)

    async def _write_all() -> None:
        payloads = [
            _file_reservation_payload(
                project,
                reservation,
                agent,
                reason_override=reason_override,
            )
            for reservation, agent in records
        ]
        await write_file_reservation_records(target_archive, payloads)

    if archive_locked:
        await _write_all()
        return

    async with _archive_write_lock(target_archive):
        await _write_all()


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
            .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
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
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, Message.sender_id).in_(agent_ids),
                )
                .group_by(Message.sender_id)
            )
            send_map = {row[0]: _ensure_utc(row[1]) for row in send_result}
            ack_result = await session.execute(
                select(MessageRecipient.agent_id, func.max(MessageRecipient.ack_ts))
                .join(Message, MessageRecipient.message_id == Message.id)
                .where(
                    cast(Any, Message.project_id) == project.id,
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
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, MessageRecipient.agent_id).in_(agent_ids),
                    cast(Any, MessageRecipient.read_ts).is_not(None),
                )
                .group_by(MessageRecipient.agent_id)
            )
            read_map = {row[0]: _ensure_utc(row[1]) for row in read_result}

    workspace = _project_workspace_path(project)
    repo = _open_repo_if_available(workspace) if workspace is not None else None

    statuses: list[FileReservationStatus] = []
    try:
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
    finally:
        # Cleanup: close repo if we opened one
        if repo is not None:
            with suppress(Exception):
                repo.close()
    return statuses


async def _expire_stale_file_reservations(
    project_id: int,
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
) -> list[FileReservationStatus]:
    await ensure_schema()
    now = datetime.now(timezone.utc)
    naive_now = _naive_utc(now)  # Compute once for consistency and efficiency

    project: Optional[Project] = None
    async with get_session() as session:
        project = await session.get(Project, project_id)
    if project is None:
        return []

    expired_pairs: list[tuple[FileReservation, Agent]] = []
    # Release any entries whose TTL has already elapsed
    async with get_session() as session:
        expired_rows = await session.execute(
            select(FileReservation, Agent)
            .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
            .where(
                cast(Any, FileReservation.project_id) == project_id,
                cast(Any, FileReservation.released_ts).is_(None),
                cast(Any, FileReservation.expires_ts) < naive_now,  # SQLite needs naive datetime
            )
        )
        expired_pairs = [cast(tuple[FileReservation, Agent], row) for row in expired_rows.all()]
        if expired_pairs:
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.project_id) == project_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                    cast(Any, FileReservation.expires_ts) < naive_now,  # SQLite needs naive datetime
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()
    statuses = await _collect_file_reservation_statuses(project, include_released=False, now=now)
    stale_statuses = [status for status in statuses if status.stale and status.reservation.id is not None]
    stale_ids = [cast(int, status.reservation.id) for status in stale_statuses]
    if stale_ids:
        async with get_session() as session:
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.project_id) == project_id,
                    cast(Any, FileReservation.id).in_(stale_ids),
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()

        for status in stale_statuses:
            status.reservation.released_ts = naive_now

    for reservation, _agent in expired_pairs:
        reservation.released_ts = naive_now

    released_pairs: list[tuple[FileReservation, Agent]] = []
    seen_ids: set[int] = set()
    for reservation, agent in expired_pairs:
        if reservation.id is None:
            continue
        if reservation.id in seen_ids:
            continue
        seen_ids.add(reservation.id)
        released_pairs.append((reservation, agent))
    for status in stale_statuses:
        if status.reservation.id is None:
            continue
        if status.reservation.id in seen_ids:
            continue
        seen_ids.add(status.reservation.id)
        released_pairs.append((status.reservation, status.agent))

    if released_pairs:
        await _write_file_reservation_records(
            project,
            released_pairs,
            archive=archive,
            archive_locked=archive_locked,
        )

    return stale_statuses


def _file_reservations_conflict(existing: FileReservation, candidate_path: str, candidate_exclusive: bool, candidate_agent: Agent) -> bool:
    if existing.released_ts is not None:
        return False
    if existing.agent_id == candidate_agent.id:
        return False
    if not existing.exclusive and not candidate_exclusive:
        return False
    # Git wildmatch semantics; treat inputs as repo-root relative forward-slash paths
    def _normalize(p: str) -> str:
        return p.replace("\\", "/").lstrip("/")
    candidate_norm = _normalize(candidate_path)
    existing_norm = _normalize(existing.path_pattern)
    # If either side is a glob, treat both as patterns and check for overlap conservatively
    if _contains_glob(candidate_norm) or _contains_glob(existing_norm):
        return _patterns_overlap(existing_norm, candidate_norm)
    if PathSpec is not None:
        spec = _compile_pathspec(_normalize_pathspec_pattern(existing.path_pattern))
        if spec is not None:
            return spec.match_file(candidate_norm)
    # Fallback to conservative fnmatch if pathspec not available
    a = candidate_norm
    b = existing_norm
    return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a) or (a == b)


def _normalize_pathspec_pattern(pattern: str) -> str:
    """Normalize a pattern for PathSpec caching (slash normalization + leading slash strip)."""
    return pattern.replace("\\", "/").lstrip("/")


@functools.lru_cache(maxsize=1024)
def _compile_pathspec(pattern: str) -> "PathSpec | None":
    """Compile a PathSpec from a normalized pattern with LRU caching.

    Returns None if PathSpec is not available.
    """
    if PathSpec is None:
        return None
    return PathSpec.from_lines("gitignore", [pattern])


def _patterns_overlap(a: str, b: str) -> bool:
    # Overlap if any file could be matched by both patterns (approximate by cross-matching)
    a_norm = _normalize_pathspec_pattern(a)
    b_norm = _normalize_pathspec_pattern(b)

    a_spec = _compile_pathspec(a_norm)
    b_spec = _compile_pathspec(b_norm)

    if a_spec is not None and b_spec is not None:
        # Heuristic: check direct cross-matches on normalized patterns
        return a_spec.match_file(b_norm) or b_spec.match_file(a_norm)
    # Fallback approximate
    return fnmatch.fnmatchcase(a_norm, b_norm) or fnmatch.fnmatchcase(b_norm, a_norm) or (a_norm == b_norm)


def _file_reservations_patterns_overlap(paths_a: Sequence[str], paths_b: Sequence[str]) -> bool:
    for pa in paths_a:
        for pb in paths_b:
            if _patterns_overlap(pa, pb):
                return True
    return False


_ARCHIVE_PATH_PREFIXES: tuple[str, ...] = (
    "agents/",
    "messages/",
    "attachments/",
    "threads/",
    "file_reservations/",
)


def _looks_like_archive_path(pattern: str) -> bool:
    """Return True if a reservation pattern targets archive paths (agents/, messages/, attachments/...)."""
    normalized = (pattern or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return normalized.startswith(_ARCHIVE_PATH_PREFIXES)


def _build_reservation_union_spec(
    existing_reservations: list[tuple["FileReservation", str]],
    exclude_agent_id: int | None,
    candidate_exclusive: bool,
) -> "PathSpec | None":
    """Build a union PathSpec matching ANY potentially conflicting reservation pattern.

    This enables O(n+m) conflict detection instead of O(n*m) by quickly identifying
    which candidate paths MIGHT conflict with existing reservations.

    Parameters
    ----------
    existing_reservations : list[tuple[FileReservation, str]]
        List of (reservation, holder_name) tuples to check against.
    exclude_agent_id : int | None
        Agent ID to exclude (the requesting agent's own reservations), or None.
    candidate_exclusive : bool
        Whether the candidate reservation is exclusive.

    Returns
    -------
    PathSpec | None
        A union PathSpec matching any potentially conflicting pattern, or None if
        no patterns qualify or PathSpec is unavailable.

    Notes
    -----
    A reservation is potentially conflicting if:
    - It is not released (released_ts is None)
    - It belongs to a different agent (agent_id != exclude_agent_id)
    - Either the existing or candidate reservation is exclusive
    """
    if PathSpec is None:
        return None

    patterns: list[str] = []
    for record, _ in existing_reservations:
        # Skip released reservations
        if record.released_ts is not None:
            continue
        # Skip own reservations
        if record.agent_id == exclude_agent_id:
            continue
        # Skip non-exclusive if candidate is also non-exclusive
        if not record.exclusive and not candidate_exclusive:
            continue
        # Add normalized pattern
        patterns.append(_normalize_pathspec_pattern(record.path_pattern))

    if not patterns:
        return None

    # Build union PathSpec matching ANY of these patterns
    return PathSpec.from_lines("gitignore", patterns)


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
            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
            .where(
                cast(Any, Message.project_id) == project.id,
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
                stmt = stmt.where(Message.created_ts > _naive_utc(since_dt))
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
                stmt = stmt.where(Message.created_ts > _naive_utc(since_dt))
        result = await session.execute(stmt)
        message_rows = result.scalars().all()

        if not message_rows:
            return messages

        # Batch fetch all recipients for all messages in one query (N+1 elimination)
        message_ids = [msg.id for msg in message_rows if msg.id is not None]
        if not message_ids:
            message_ids = []
        recs_stmt = (
            select(MessageRecipient.message_id, MessageRecipient.kind, Agent.name)
            .join(Agent, MessageRecipient.agent_id == Agent.id)
            .where(cast(Any, MessageRecipient.message_id).in_(message_ids))
        )
        recs_result = await session.execute(recs_stmt)
        all_recipients = recs_result.all()

        # Group recipients by message_id
        recipients_by_msg: dict[int, dict[str, list[str]]] = {}
        for msg_id, kind, name in all_recipients:
            if msg_id not in recipients_by_msg:
                recipients_by_msg[msg_id] = {"to": [], "cc": [], "bcc": []}
            if kind in ("to", "cc", "bcc"):
                recipients_by_msg[msg_id][kind].append(name)

        # Build output
        for msg in message_rows:
            if msg.id is None:
                continue
            payload = _message_to_dict(msg, include_body=include_bodies)
            payload["from"] = agent.name
            rec_data = recipients_by_msg.get(msg.id, {"to": [], "cc": [], "bcc": []})
            payload["to"] = rec_data["to"]
            payload["cc"] = rec_data["cc"]
            payload["bcc"] = rec_data["bcc"]
            messages.append(payload)
    return messages


def _canonical_relpath_for_message(project: Project, message: Message, archive: ProjectArchive) -> str | None:
    """Resolve the canonical repo-relative path for a message markdown file.

    Supports both legacy filenames ("<id>.md") and the new descriptive pattern
    ("<ISO>__<subject-slug>__<id>.md"). Returns a path relative to the archive
    Git repo root, or None if no matching file is found.
    """
    ts = _ensure_utc(message.created_ts)
    if ts is None:
        return None
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

    def _lookup() -> dict[str, Any] | None:
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
    criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
    if message_id is not None:
        criteria.append(cast(Any, Message.id) == message_id)
    async with get_session() as session:
        stmt = (
            select(Message, sender_alias.name)
            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
            .where(cast(Any, Message.project_id) == project.id, or_(*criteria))
            .order_by(asc(cast(Any, Message.created_ts)))
        )
        if per_thread_limit:
            stmt = stmt.limit(per_thread_limit)
        result = await session.execute(stmt)
        raw_rows = result.all()
    rows = [(row[0], row[1]) for row in raw_rows]
    summary = _summarize_messages(rows)
    heuristic_key_points = list(summary.get("key_points", []))

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
                    if heuristic_key_points and isinstance(summary.get("key_points"), list):
                        keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")
                        extra = [
                            kp for kp in heuristic_key_points
                            if any(token in str(kp).upper() for token in keywords)
                        ]
                        if extra:
                            merged: list[str] = []
                            for item in summary["key_points"] + extra:
                                if item not in merged:
                                    merged.append(item)
                            summary["key_points"] = merged[:10]
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
    naive_now = _naive_utc(now)  # Use naive UTC for SQLite compatibility
    async with get_session() as session:
        # Read current value first
        result_sel = await session.execute(
            select(MessageRecipient).where(cast(Any, MessageRecipient.message_id == message_id), cast(Any, MessageRecipient.agent_id == agent.id))
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
            .values({field: naive_now})
        )
        await session.execute(stmt)
        await session.commit()
    return naive_now


def build_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server instance."""
    settings: Settings = get_settings()
    lifespan = _lifespan_factory(settings)

    instructions = (
        "You are the MCP Agent Mail coordination server. "
        "Provide message routing, coordination tooling, and project context to cooperating agents. "
        "Outputs are JSON by default; pass format='toon' (or set MCP_AGENT_MAIL_OUTPUT_FORMAT=toon) to receive "
        "{format:'toon', data:'<TOON>'}."
    )

    mcp = FastMCP(name="mcp-agent-mail", instructions=instructions, lifespan=lifespan)

    async def _ctx_info_safe(ctx: Context, message: str) -> None:
        try:
            await ctx.info(message)
        except Exception:
            # Context may not be available outside of a request; ignore logging
            return

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
        combined_names = [*to_names, *cc_names, *bcc_names]
        agent_map = await _get_agents_batch(project, combined_names)
        to_agents = [agent_map[name] for name in to_names]
        cc_agents = [agent_map[name] for name in cc_names]
        bcc_agents = [agent_map[name] for name in bcc_names]
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
                await _expire_stale_file_reservations(
                    project.id or 0,
                    archive=archive,
                    archive_locked=True,
                )
                now_ts = datetime.now(timezone.utc)
                y_dir = now_ts.strftime("%Y")
                m_dir = now_ts.strftime("%m")
                candidate_surfaces: list[str] = []
                candidate_surfaces.append(f"messages/{y_dir}/{m_dir}/*.md")
                candidate_surfaces.append(f"agents/{sender.name}/outbox/{y_dir}/{m_dir}/*.md")
                for r in to_agents + cc_agents + bcc_agents:
                    candidate_surfaces.append(f"agents/{r.name}/inbox/{y_dir}/{m_dir}/*.md")
                if thread_id:
                    candidate_surfaces.append(f"messages/threads/{thread_id}.md")
                has_attachments = bool(attachment_paths) or (
                    convert_markdown and ("![" in body_md or "data:image" in body_md)
                )
                if has_attachments:
                    candidate_surfaces.append("attachments/**")

                async with get_session() as session:
                    rows = await session.execute(
                        select(FileReservation, Agent.name)
                        .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                        .where(
                            cast(Any, FileReservation.project_id) == project.id,
                            cast(Any, FileReservation.released_ts).is_(None),
                            cast(Any, FileReservation.expires_ts) > _naive_utc(now_ts),
                        )
                    )
                    active_file_reservations: list[tuple[FileReservation, str]] = [
                        (row[0], row[1]) for row in rows.all()
                    ]

                conflicts: list[dict[str, Any]] = []

                archive_reservations = [
                    (reservation, holder)
                    for reservation, holder in active_file_reservations
                    if _looks_like_archive_path(reservation.path_pattern)
                ]
                if not archive_reservations:
                    conflicts = []
                else:
                    # Build union PathSpec for fast conflict pre-filtering
                    union_spec = _build_reservation_union_spec(archive_reservations, sender.id, True)

                    # Pre-compute which surfaces might conflict
                    potentially_conflicting_surfaces: set[str] = set()
                    if union_spec is not None:
                        normalized_surfaces = [_normalize_pathspec_pattern(s) for s in candidate_surfaces]
                        matching_normalized = set(union_spec.match_files(normalized_surfaces))
                        for orig_surface, norm_surface in zip(candidate_surfaces, normalized_surfaces, strict=True):
                            if norm_surface in matching_normalized:
                                potentially_conflicting_surfaces.add(orig_surface)
                    else:
                        potentially_conflicting_surfaces = set(candidate_surfaces)

                    for surface in candidate_surfaces:
                        if surface not in potentially_conflicting_surfaces:
                            continue  # Fast path: no conflicts possible for this surface
                        for file_reservation_record, holder_name in archive_reservations:
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

            # Emit notification signals for recipients (if enabled)
            if settings.notifications.enabled:
                message_meta = {
                    "id": message.id,
                    "from": sender.name,
                    "subject": subject,
                    "importance": importance,
                }
                # Signal to/cc recipients (not bcc - blind copies shouldn't trigger visible signals)
                for agent in to_agents + cc_agents:
                    with suppress(Exception):
                        await emit_notification_signal(
                            settings,
                            project.slug,
                            agent.name,
                            message_meta,
                        )

        await ctx.info(
            f"Message {message.id} created by {sender.name} (to {', '.join(recipients_for_archive)})"
        )
        if payload is None:
            raise RuntimeError("Message payload was not generated.")
        return payload

    @mcp.tool(name="health_check", description="Return basic readiness information for the Agent Mail server.")
    @_instrument_tool("health_check", cluster=CLUSTER_SETUP, capabilities={"infrastructure"}, complexity="low")
    async def health_check(ctx: Context, format: Optional[str] = None) -> dict[str, Any]:
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
    async def ensure_project(
        ctx: Context,
        human_key: str,
        identity_mode: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
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

        await _ctx_info_safe(ctx, f"Ensuring project for key '{human_key}'.")
        project = await _ensure_project(human_key)
        await ensure_archive(settings, project.slug)
        payload = _project_to_dict(project)
        # Worktree identity metadata is opt-in to keep default calls lightweight and stable.
        if settings.worktrees_enabled:
            payload.update(_resolve_project_identity(human_key))
        return payload

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
        format: Optional[str] = None,
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
        _validate_program_model(program, model)
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
        format: Optional[str] = None,
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
        format: Optional[str] = None,
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
        _validate_program_model(program, model)
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
        format: Optional[str] = None,
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
            Note: sender attachments_policy "inline"/"file" always forces conversion/inlining.
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

        # Normalize 'to' parameter - accept single string and convert to list
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list):
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                f"'to' must be a list of agent names (e.g., ['BlueLake']) or a single agent name string. "
                f"Received: {type(to).__name__}",
                recoverable=True,
                data={"argument": "to", "received_type": type(to).__name__},
            )

        # Check for common recipient mistakes and provide helpful guidance
        for recipient in to:
            if not isinstance(recipient, str):
                raise ToolExecutionError(
                    "INVALID_ARGUMENT",
                    f"Each recipient in 'to' must be a string (agent name). Got: {type(recipient).__name__}",
                    recoverable=True,
                    data={"argument": "to", "invalid_item": repr(recipient)},
                )
            mistake = _detect_agent_name_mistake(recipient)
            if mistake:
                raise ToolExecutionError(
                    mistake[0],
                    f"Invalid recipient '{recipient}': {mistake[1]}",
                    recoverable=True,
                    data={"recipient": recipient, "hint": "Use agent names like 'BlueLake', not program/model names"},
                )

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

        # Self-send detection: warn if sender is sending to themselves
        sender_lower = sender_name.lower().strip()
        all_recipients = (to or []) + (cc or []) + (bcc or [])
        self_send_matches = [r for r in all_recipients if r.lower().strip() == sender_lower]
        if self_send_matches:
            await ctx.info(
                f"[note] You ({sender_name}) are sending a message to yourself. "
                f"This is allowed but usually not intended. To communicate with other agents, "
                f"use their agent names (e.g., 'BlueLake'). To discover agents, "
                f"use resource://agents/{project_key}."
            )

        # Subject length warning: warn if subject is too long (will be truncated in DB)
        if len(subject) > 200:
            await ctx.info(
                f"[warn] Subject is {len(subject)} characters (max recommended: 80, truncated at 200). "
                f"Long subjects may be truncated in search results. Consider moving details to the message body."
            )
            subject = subject[:200]

        thread_id = _validate_thread_id(thread_id)

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
        if settings_local.contact_enforcement_enabled:
            # allow replies always; if thread present and recipient already on thread, allow
            auto_ok_names: set[str] = set()
            if thread_id:
                try:
                    thread_rows: list[tuple[Message, str]]
                    sender_alias = aliased(Agent)
                    # Build criteria: thread_id match or numeric id seed
                    criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
                    try:
                        seed_id = int(thread_id)
                        criteria.append(cast(Any, Message.id) == seed_id)
                    except Exception:
                        pass
                    async with get_session() as s:
                        stmt = (
                            select(Message, sender_alias.name)
                            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                            .where(cast(Any, Message.project_id) == project.id, or_(*criteria))
                            .limit(500)
                        )
                        thread_rows = [(row[0], row[1]) for row in (await s.execute(stmt)).all()]
                    # collect participants (sender names and recipients)
                    participants: set[str] = {n for _m, n in thread_rows}
                    message_ids = [m.id for m, _ in thread_rows if m.id is not None]
                    if message_ids:
                        recipient_rows = await s.execute(
                            select(Agent.name)
                            .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                            .where(cast(Any, MessageRecipient.message_id).in_(message_ids))
                        )
                        participants.update({row[0] for row in recipient_rows.all() if row[0]})
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
                        .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                        .where(FileReservation.project_id == project.id, cast(Any, FileReservation.released_ts).is_(None), cast(Any, FileReservation.expires_ts) > _naive_utc(now_utc))
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
            # Batch-fetch all recipient agents in a single query (eliminates N+1)
            all_recipient_names = list(set(to + (cc or []) + (bcc or [])))
            recipient_agents = await _get_agents_batch_lenient(project, all_recipient_names)
            async with get_session() as s3:
                recent_ok_names: set[str] = set()
                ttl = timedelta(seconds=int(settings_local.contact_auto_ttl_seconds))
                since_dt = now_utc - ttl
                # Batch fetch recent contacts (sender -> recipients and recipients -> sender)
                try:
                    recipient_name_filter = list(all_recipient_names)
                    if recipient_name_filter:
                        sent_stmt = (
                            select(Agent.name)
                            .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                            .join(Message, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, Message.sender_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, Agent.name).in_(recipient_name_filter),
                            )
                        )
                        sent_rows = await s3.execute(sent_stmt)
                        recent_ok_names.update({row[0] for row in sent_rows.all() if row[0]})

                        sender_alias2 = aliased(Agent)
                        recv_stmt = (
                            select(sender_alias2.name)
                            .join(Message, cast(Any, Message.sender_id) == sender_alias2.id)
                            .join(MessageRecipient, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, MessageRecipient.agent_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, sender_alias2.name).in_(recipient_name_filter),
                            )
                        )
                        recv_rows = await s3.execute(recv_stmt)
                        recent_ok_names.update({row[0] for row in recv_rows.all() if row[0]})
                except Exception:
                    recent_ok_names = set()
                # Batch fetch approved agent links for these recipients
                approved_link_ids: set[int] = set()
                try:
                    recipient_ids = [rec.id for rec in recipient_agents.values() if rec is not None and rec.id is not None]
                    if recipient_ids:
                        link_rows = await s3.execute(
                            select(AgentLink.b_agent_id)
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.b_project_id) == project.id,
                                cast(Any, AgentLink.status == "approved"),
                                cast(Any, AgentLink.b_agent_id).in_(recipient_ids),
                            )
                        )
                        approved_link_ids.update({row[0] for row in link_rows.all() if row and row[0] is not None})
                except Exception:
                    approved_link_ids = set()

                for nm in to + (cc or []) + (bcc or []):
                    if nm in auto_ok_names:
                        continue
                    # recipient lookup (from batch-fetched dict)
                    rec = recipient_agents.get(nm)
                    if rec is None:
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
                    recent_ok = rec.name in recent_ok_names
                    if rec_policy == "auto" and recent_ok:
                        continue
                    # check approved AgentLink (local project)
                    if rec.id is not None and rec.id in approved_link_ids:
                        continue
                    # Contact policy must be enforced regardless of ack_required flag.
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
                        from fastmcp.tools.tool import FunctionTool
                        # Prefer a single handshake with auto_accept=true
                        handshake = cast(FunctionTool, cast(Any, macro_contact_handshake))
                        for nm in blocked_recipients:
                            try:
                                await handshake.run(
                                    {
                                        "ctx": ctx,
                                        "project_key": project.human_key,
                                        "requester": sender.name,
                                        "target": nm,
                                        "reason": "auto-handshake by send_message",
                                        "auto_accept": True,
                                        "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                        "format": "json",
                                    }
                                )
                                attempted.append(nm)
                            except Exception:
                                pass

                        # If auto-retry is enabled and at least one handshake happened, re-evaluate recipients once
                        if settings_local.contact_auto_retry_enabled and attempted:
                            blocked_recipients = []
                            # Re-fetch recipient agents in batch for re-evaluation
                            recipient_agents_retry = await _get_agents_batch_lenient(project, all_recipient_names)
                            async with get_session() as s3b:
                                for nm in to + (cc or []) + (bcc or []):
                                    rec = recipient_agents_retry.get(nm)
                                    if rec is None:
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
                                            cast(Any, AgentLink.a_project_id) == project.id,
                                            cast(Any, AgentLink.a_agent_id) == sender.id,
                                            cast(Any, AgentLink.b_project_id) == project.id,
                                            cast(Any, AgentLink.b_agent_id) == rec.id,
                                            cast(Any, AgentLink.status == "approved"),
                                        )
                                        .limit(1)
                                    )
                                    if link.first() is None:
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
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.status == "approved"),
                                cast(Any, Project.id == target_project_override.id),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.status == "approved"),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )

                    rec = rows.first() if rows else None
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
                                settings,
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
                        from fastmcp.tools.tool import FunctionTool
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
                                            "ctx": ctx,
                                            "project_key": project.human_key,
                                            "requester": sender.name,
                                            "target": nm,
                                            "to_project": target_proj.human_key or target_proj.slug,
                                            "reason": "auto-handshake by send_message",
                                            "auto_accept": True,
                                            "ttl_seconds": int(settings_local.contact_auto_ttl_seconds),
                                            "register_if_missing": True,
                                            "format": "json",
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
                                                .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                                                .where(
                                                    cast(Any, AgentLink.a_project_id) == project.id,
                                                    cast(Any, AgentLink.a_agent_id) == sender.id,
                                                    cast(Any, AgentLink.status == "approved"),
                                                    cast(Any, Project.id == tproj.id),
                                                    cast(Any, func.lower(Agent.name) == lookup_value),
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
                alias = await _get_or_create_agent(p, sender.name, sender.program, sender.model, sender.task_description, settings)
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
        format: Optional[str] = None,
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
            local_names = {row[0] for row in existing.fetchall()}

            class _ContactBlocked(Exception):
                pass

            async def _route(name_list: list[str], kind: str) -> None:
                for nm in name_list:
                    target_project_override: Project | None = None
                    target_name_override: str | None = None
                    if nm.startswith("project:") and "#" in nm:
                        try:
                            _, rest = nm.split(":", 1)
                            slug_part, agent_part = rest.split("#", 1)
                            target_project_override = await _get_project_by_identifier(slug_part)
                            target_name_override = agent_part.strip()
                        except Exception:
                            target_project_override = None
                            target_name_override = None
                    if nm in local_names:
                        if kind == "to":
                            local_to.append(nm)
                        elif kind == "cc":
                            local_cc.append(nm)
                        else:
                            local_bcc.append(nm)
                        continue
                    rows = None
                    if target_project_override is not None and target_name_override:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.status == "approved"),
                                cast(Any, Project.id == target_project_override.id),
                                cast(Any, Agent.name == target_name_override),
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.status == "approved"),
                                cast(Any, Agent.name == nm),
                            )
                            .limit(1)
                        )
                    rec = rows.first()
                    if rec:
                        _link, target_project, target_agent = rec
                        recipient_policy = (getattr(target_agent, "contact_policy", "auto") or "auto").lower()
                        if recipient_policy == "block_all":
                            await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                            raise _ContactBlocked()
                        bucket = external.setdefault(target_project.id or 0, {"project": target_project, "to": [], "cc": [], "bcc": []})
                        bucket[kind].append(target_agent.name)
                    else:
                        if kind == "to":
                            local_to.append(nm)
                        elif kind == "cc":
                            local_cc.append(nm)
                        else:
                            local_bcc.append(nm)

        try:
            await _route(to_names, "to")
            await _route(cc_list, "cc")
            await _route(bcc_list, "bcc")
        except _ContactBlocked:
            return {"error": {"type": "CONTACT_BLOCKED", "message": "Recipient is not accepting messages."}}

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
        format: Optional[str] = None,
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
        except (NoResultFound, ToolExecutionError) as exc:
            # Check if this is a NOT_FOUND error we can handle with register_if_missing
            is_not_found = isinstance(exc, NoResultFound) or (
                isinstance(exc, ToolExecutionError) and exc.error_type == "NOT_FOUND"
            )
            if is_not_found and register_if_missing and validate_agent_name_format(target_name):
                # Create the missing target identity using provided metadata (best effort)
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
        # Warn on TTL auto-correction
        if ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
            )
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        exp = naive_now + timedelta(seconds=max(60, ttl_seconds))
        should_notify = False
        async with get_session() as s:
            # upsert link
            existing = await s.execute(
                select(AgentLink).where(
                    cast(Any, AgentLink.a_project_id) == project.id,
                    cast(Any, AgentLink.a_agent_id) == a.id,
                    cast(Any, AgentLink.b_project_id) == target_project.id,
                    cast(Any, AgentLink.b_agent_id) == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                previous_status = link.status
                link.status = "pending"
                link.reason = reason
                link.updated_ts = naive_now
                link.expires_ts = exp
                s.add(link)
                should_notify = previous_status != "pending"
            else:
                link = AgentLink(
                    a_project_id=project.id or 0,
                    a_agent_id=a.id or 0,
                    b_project_id=target_project.id or 0,
                    b_agent_id=b.id or 0,
                    status="pending",
                    reason=reason,
                    created_ts=naive_now,
                    updated_ts=naive_now,
                    expires_ts=exp,
                )
                s.add(link)
                should_notify = True
            try:
                await s.commit()
            except IntegrityError:
                # Another concurrent request created the link. Treat this as an idempotent refresh.
                await s.rollback()
                existing = await s.execute(
                    select(AgentLink).where(
                        cast(Any, AgentLink.a_project_id) == project.id,
                        cast(Any, AgentLink.a_agent_id) == a.id,
                        cast(Any, AgentLink.b_project_id) == target_project.id,
                        cast(Any, AgentLink.b_agent_id) == b.id,
                    )
                )
                link = existing.scalars().first()
                if link is None:
                    raise
                link.status = "pending"
                link.reason = reason
                link.updated_ts = naive_now
                link.expires_ts = exp
                s.add(link)
                await s.commit()
                should_notify = False

        if should_notify:
            # Send an intro message with ack_required.
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
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Approve or deny a contact request."""
        project = await _get_project_by_identifier(project_key)
        # Resolve remote requestor project if provided
        a_project = project if not from_project else await _get_project_by_identifier(from_project)
        a = await _get_agent(a_project, from_agent)
        b = await _get_agent(project, to_agent)
        # Warn on TTL auto-correction
        if accept and ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
            )
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        exp = naive_now + timedelta(seconds=max(60, ttl_seconds)) if accept else None
        updated = 0
        async with get_session() as s:
            existing = await s.execute(
                select(AgentLink).where(
                    cast(Any, AgentLink.a_project_id) == a_project.id,
                    cast(Any, AgentLink.a_agent_id) == a.id,
                    cast(Any, AgentLink.b_project_id) == project.id,
                    cast(Any, AgentLink.b_agent_id) == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                link.status = "approved" if accept else "blocked"
                link.updated_ts = naive_now
                link.expires_ts = exp
                s.add(link)
                updated = 1
            else:
                if accept:
                    if a_project.id is None or a.id is None or project.id is None or b.id is None:
                        raise ValueError("Projects and agents must have ids before creating contact links.")
                    s.add(AgentLink(
                        a_project_id=a_project.id,
                        a_agent_id=a.id,
                        b_project_id=project.id,
                        b_agent_id=b.id,
                        status="approved",
                        reason="",
                        created_ts=naive_now,
                        updated_ts=naive_now,
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
    async def list_contacts(
        ctx: Context,
        project_key: str,
        agent_name: str,
        format: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List contact links for an agent in a project."""
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        out: list[dict[str, Any]] = []
        async with get_session() as s:
            rows = await s.execute(
                select(AgentLink, Agent.name)
                .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                .where(cast(Any, AgentLink.a_project_id) == project.id, cast(Any, AgentLink.a_agent_id) == agent.id)
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
    async def set_contact_policy(
        ctx: Context,
        project_key: str,
        agent_name: str,
        policy: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
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
        format: Optional[str] = None,
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
        # Validate limit parameter bounds
        if limit < 1:
            raise ToolExecutionError(
                error_type="INVALID_LIMIT",
                message=f"limit must be at least 1, got {limit}. Use a positive integer.",
                recoverable=True,
                data={"provided": limit, "min": 1, "max": 1000},
            )
        if limit > 1000:
            await ctx.info(f"[warn] limit={limit} is very large; capping at 1000 to prevent performance issues.")
            limit = 1000

        # Validate since_ts format upfront with helpful error message
        _validate_iso_timestamp(since_ts, "since_ts")

        settings = get_settings()
        if settings.tools_log_enabled:
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
            if settings.notifications.enabled:
                with suppress(Exception):
                    await clear_notification_signal(settings, project.slug, agent.name)
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
        format: Optional[str] = None,
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
                    from rich.console import Console
                    from rich.json import JSON

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
        format: Optional[str] = None,
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
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Macro helper that boots a project session: ensure project, register agent,
        optionally file_reservation paths, and fetch the latest inbox snapshot.
        """
        _validate_program_model(program, model)
        settings = get_settings()
        project = await _ensure_project(human_key)
        agent = await _get_or_create_agent(project, agent_name, program, model, task_description, settings)

        file_reservations_result: Optional[dict[str, Any]] = None
        if file_reservation_paths:
            # Use MCP tool registry to avoid param shadowing (file_reservation_paths param shadows file_reservation_paths function)
            from fastmcp.tools.tool import FunctionTool

            mcp_with_tools = cast(_FastMCPToolGetter, mcp)
            _file_reservation_tool = cast(FunctionTool, await mcp_with_tools.get_tool("file_reservation_paths"))
            _file_reservation_run = await _file_reservation_tool.run(
                {
                    "ctx": ctx,
                    "project_key": project.human_key,
                    "agent_name": agent.name,
                    "paths": file_reservation_paths,
                    "ttl_seconds": file_reservation_ttl_seconds,
                    "exclusive": True,
                    "reason": file_reservation_reason,
                    "format": "json",
                }
            )
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
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Macro helper that aligns an agent with an existing thread by ensuring registration,
        summarising the thread, and fetching recent inbox context.
        """
        settings = get_settings()
        project = await _get_project_by_identifier(project_key)
        if register_if_missing:
            _validate_program_model(program, model)
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
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reserve a set of file paths and optionally release them at the end of the workflow."""

        # Call underlying FunctionTool directly so we don't treat the wrapper as a plain coroutine
        from fastmcp.tools.tool import FunctionTool

        file_reservations_tool = cast(FunctionTool, cast(Any, file_reservation_paths))
        file_reservations_tool_result = await file_reservations_tool.run(
            {
                "ctx": ctx,
                "project_key": project_key,
                "agent_name": agent_name,
                "paths": paths,
                "ttl_seconds": ttl_seconds,
                "exclusive": exclusive,
                "reason": reason,
                "format": "json",
            }
        )
        file_reservations_result = cast(dict[str, Any], file_reservations_tool_result.structured_content or {})

        release_result = None
        if auto_release:
            release_tool = cast(FunctionTool, cast(Any, release_file_reservations_tool))
            release_tool_result = await release_tool.run(
                {
                    "ctx": ctx,
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "paths": paths,
                    "format": "json",
                }
            )
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
        format: Optional[str] = None,
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
                        rows = await s.execute(select(Agent.name).where(cast(Any, Agent.project_id) == project.id))
                        names = [str(row[0]).strip() for row in rows.fetchall() if (row and row[0])]
                    if len(names) == 1:
                        real_requester = names[0]
                # If target missing and exactly two agents exist, infer the other
                if not real_target and project.id is not None:
                    async with get_session() as s2:
                        rows2 = await s2.execute(select(Agent.name).where(cast(Any, Agent.project_id) == project.id))
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

        # Fast path: for same-project auto-accept handshakes (used heavily by send_message),
        # approve the AgentLink directly without generating extra "intro" messages.
        if auto_accept and not target_project_key and not (welcome_subject and welcome_body):
            project = await _get_project_by_identifier(project_key)
            a = await _get_agent(project, real_requester)
            try:
                b = await _get_agent(project, real_target)
            except (NoResultFound, ToolExecutionError) as exc:
                is_not_found = isinstance(exc, NoResultFound) or (
                    isinstance(exc, ToolExecutionError) and exc.error_type == "NOT_FOUND"
                )
                if is_not_found and register_if_missing and validate_agent_name_format(real_target):
                    settings = get_settings()
                    b = await _get_or_create_agent(
                        project,
                        real_target,
                        program or "unknown",
                        model or "unknown",
                        task_description or "",
                        settings,
                    )
                else:
                    raise

            if ttl_seconds < 60:
                await ctx.info(
                    f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
                )
            now = datetime.now(timezone.utc)
            naive_now = _naive_utc(now)
            exp = naive_now + timedelta(seconds=max(60, ttl_seconds))

            async with get_session() as s:
                existing = await s.execute(
                    select(AgentLink).where(
                        cast(Any, AgentLink.a_project_id) == project.id,
                        cast(Any, AgentLink.a_agent_id) == a.id,
                        cast(Any, AgentLink.b_project_id) == project.id,
                        cast(Any, AgentLink.b_agent_id) == b.id,
                    )
                )
                link = existing.scalars().first()
                if link:
                    link.status = "approved"
                    link.reason = reason
                    link.updated_ts = naive_now
                    link.expires_ts = exp
                    s.add(link)
                else:
                    link = AgentLink(
                        a_project_id=project.id or 0,
                        a_agent_id=a.id or 0,
                        b_project_id=project.id or 0,
                        b_agent_id=b.id or 0,
                        status="approved",
                        reason=reason,
                        created_ts=naive_now,
                        updated_ts=naive_now,
                        expires_ts=exp,
                    )
                    s.add(link)
                try:
                    await s.commit()
                except IntegrityError:
                    # Another concurrent handshake created the link; treat as idempotent approval.
                    await s.rollback()
                    existing = await s.execute(
                        select(AgentLink).where(
                            cast(Any, AgentLink.a_project_id) == project.id,
                            cast(Any, AgentLink.a_agent_id) == a.id,
                            cast(Any, AgentLink.b_project_id) == project.id,
                            cast(Any, AgentLink.b_agent_id) == b.id,
                        )
                    )
                    link = existing.scalars().first()
                    if link is None:
                        raise
                    link.status = "approved"
                    link.reason = reason
                    link.updated_ts = naive_now
                    link.expires_ts = exp
                    s.add(link)
                    await s.commit()

            approved_payload = {
                "from": a.name,
                "from_project": project.human_key,
                "to": b.name,
                "to_project": project.human_key,
                "status": "approved",
                "expires_ts": _iso(exp),
            }
            return {"request": approved_payload, "response": approved_payload, "welcome_message": None}

        from fastmcp.tools.tool import FunctionTool

        request_tool = cast(FunctionTool, cast(Any, request_contact))
        request_payload: dict[str, Any] = {
            "ctx": ctx,
            "project_key": project_key,
            "from_agent": real_requester,
            "to_agent": real_target,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "format": "json",
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
                "ctx": ctx,
                "project_key": target_project_key or project_key,
                "to_agent": real_target,
                "from_agent": real_requester,
                "accept": True,
                "ttl_seconds": ttl_seconds,
                "format": "json",
            }
            if target_project_key:
                respond_payload["from_project"] = project_key
            respond_tool_result = await respond_tool.run(respond_payload)
            response_result = cast(dict[str, Any], respond_tool_result.structured_content or {})

        welcome_result = None
        if welcome_subject and welcome_body and not target_project_key:
            try:
                send_tool = cast(FunctionTool, cast(Any, send_message))
                send_tool_result = await send_tool.run(
                    {
                        "ctx": ctx,
                        "project_key": project_key,
                        "sender_name": real_requester,
                        "to": [real_target],
                        "subject": welcome_subject,
                        "body_md": welcome_body,
                        "thread_id": thread_id,
                        "format": "json",
                    }
                )
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
        format: Optional[str] = None,
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

        # Sanitize the FTS query - returns None if query can't produce results
        sanitized_query = _sanitize_fts_query(query)
        if sanitized_query is None:
            await ctx.info(f"Search query '{query}' is not searchable, returning empty results.")
            try:
                from fastmcp.tools.tool import ToolResult
                return ToolResult(structured_content={"result": []})
            except Exception:
                return []

        await ensure_schema()
        rows: list[Any] = []
        fts_failed = False
        fts_error_msg: str | None = None
        try:
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
                    {"project_id": project.id, "query": sanitized_query, "limit": limit},
                )
                rows = list(result.mappings().all())
        except Exception as fts_err:
            # FTS query syntax error - flag for fallback instead of crashing
            fts_failed = True
            fts_error_msg = str(fts_err)
            logger.warning("FTS query failed, attempting LIKE fallback", extra={"query": sanitized_query, "error": fts_error_msg})

        # Handle FTS failure with LIKE fallback (using a fresh session)
        if fts_failed:
            fallback_terms = _extract_like_terms(query)
            if not fallback_terms:
                await ctx.info(f"Search query '{query}' could not be executed (FTS syntax issue), returning empty results.")
                rows = []
            else:
                clauses = []
                params: dict[str, Any] = {"project_id": project.id, "limit": limit}
                for idx, term in enumerate(fallback_terms):
                    key = f"t{idx}"
                    params[key] = f"%{_like_escape(term)}%"
                    clauses.append(
                        f"(m.subject LIKE :{key} ESCAPE '\\\\' OR m.body_md LIKE :{key} ESCAPE '\\\\')"
                    )
                where_clause = " AND ".join(clauses)
                async with get_session() as session:
                    result = await session.execute(
                        text(
                            f"""
                            SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                                   m.thread_id, a.name AS sender_name
                            FROM messages m
                            JOIN agents a ON m.sender_id = a.id
                            WHERE m.project_id = :project_id AND {where_clause}
                            ORDER BY m.created_ts DESC
                            LIMIT :limit
                            """
                        ),
                        params,
                    )
                    rows = list(result.mappings().all())
                await ctx.info(
                    f"FTS query failed; used LIKE fallback with {len(fallback_terms)} term(s), returned {len(rows)} result(s)."
                )

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
            from fastmcp.tools.tool import ToolResult
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
        per_thread_limit: int = 50,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Extract participants, key points, and action items for one or more threads.

        Single-thread mode (thread_id is a single ID):
        - Returns detailed summary with optional example messages
        - Response: { thread_id, summary: {participants[], key_points[], action_items[]}, examples[] }

        Multi-thread mode (thread_id is comma-separated IDs like "TKT-1,TKT-2,TKT-3"):
        - Returns aggregate digest across all threads
        - Response: { threads: [{thread_id, summary}], aggregate: {top_mentions[], key_points[], action_items[]} }

        Parameters
        ----------
        project_key : str
            Project identifier.
        thread_id : str
            Single thread ID for detailed summary, OR comma-separated IDs for aggregate digest.
        include_examples : bool
            If true (single-thread mode only), include up to 3 sample messages.
        llm_mode : bool
            If true and LLM is enabled, refine the summary with AI.
        llm_model : Optional[str]
            Override model name for the LLM call.
        per_thread_limit : int
            Max messages to consider per thread (multi-thread mode).

        Examples
        --------
        Single thread:
        ```json
        {"thread_id": "TKT-123", "include_examples": true}
        ```

        Multiple threads:
        ```json
        {"thread_id": "TKT-1,TKT-2,TKT-3"}
        ```
        """
        # Detect multi-thread mode by checking for comma-separated IDs
        thread_ids = [t.strip() for t in thread_id.split(",") if t.strip()]

        if len(thread_ids) == 1:
            # Single-thread mode: detailed summary with examples
            project = await _get_project_by_identifier(project_key)
            summary, examples, total_messages = await _compute_thread_summary(
                project,
                thread_ids[0],
                include_examples,
                llm_mode,
                llm_model,
            )
            await ctx.info(
                f"Summarized thread '{thread_ids[0]}' for project '{project.human_key}' with {total_messages} messages"
            )
            return {"thread_id": thread_ids[0], "summary": summary, "examples": examples}

        # Multi-thread mode: aggregate digest
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
                criteria = [cast(Any, Message.thread_id) == tid]
                if seed_id is not None:
                    criteria.append(cast(Any, Message.id) == seed_id)
                stmt = (
                    select(Message, sender_alias.name)
                    .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                    .where(cast(Any, Message.project_id) == project.id, or_(*criteria))
                    .order_by(asc(cast(Any, Message.created_ts)))
                    .limit(per_thread_limit)
                )
                raw_rows = (await session.execute(stmt)).all()
                rows = [(row[0], row[1]) for row in raw_rows]
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
                await ctx.debug(f"summarize_thread.llm_skipped: {e}")

        await ctx.info(f"Summarized {len(thread_ids)} thread(s) for project '{project.human_key}'.")
        return {"threads": thread_summaries, "aggregate": aggregate}

    @mcp.tool(name="install_precommit_guard")
    @_instrument_tool("install_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"}, project_arg="project_key")
    async def install_precommit_guard(
        ctx: Context,
        project_key: str,
        code_repo_path: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        if not settings.worktrees_enabled:
            await ctx.info("Worktree-friendly features are disabled (WORKTREES_ENABLED=0). Skipping guard install.")
            return {"hook": ""}
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
        await _ctx_info_safe(ctx, f"Installed pre-commit guard for project '{project.human_key}' at {hook_path}.")
        return {"hook": str(hook_path)}

    @mcp.tool(name="uninstall_precommit_guard")
    @_instrument_tool("uninstall_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"})
    async def uninstall_precommit_guard(
        ctx: Context,
        code_repo_path: str,
        format: Optional[str] = None,
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
            await _ctx_info_safe(ctx, f"Removed pre-commit guard at {repo_path / '.git/hooks/pre-commit'}.")
        else:
            await _ctx_info_safe(ctx, f"No pre-commit guard to remove at {repo_path / '.git/hooks/pre-commit'}.")
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
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Request advisory file reservations (leases) on project-relative paths/globs.

        Semantics
        ---------
        - Conflicts are reported if an overlapping active exclusive reservation exists held by another agent
        - Glob matching is symmetric (`fnmatchcase(a,b)` or `fnmatchcase(b,a)`), including exact matches
        - When granted, a JSON artifact is written under `file_reservations/<sha1(path)>.json` and the DB is updated
        - TTL must be >= 60 seconds (enforced by the server settings/policy)
        - Server-side enforcement (if enabled) only checks reservations that target mail archive paths
          such as `agents/`, `messages/`, or `attachments/`; code repo enforcement is via the pre-commit guard

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
        # Validate paths is not empty
        if not paths:
            raise ToolExecutionError(
                error_type="EMPTY_PATHS",
                message=(
                    "paths list cannot be empty. Provide at least one file path or glob pattern "
                    "to reserve (e.g., ['src/api/*.py', 'config/settings.yaml'])."
                ),
                recoverable=True,
                data={"provided": paths},
            )

        # Warn on very short TTL (but still allow it for testing scenarios)
        if ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below recommended minimum (60s). "
                f"Very short TTLs may cause unexpected expiry during processing."
            )

        project = await _get_project_by_identifier(project_key)
        settings = get_settings()
        if settings.tools_log_enabled:
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
        # Validate path patterns and warn on suspicious patterns
        for pattern in paths:
            warning = _detect_suspicious_file_reservation(pattern)
            if warning:
                await ctx.info(f"[warn] {warning}")

        granted: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        archive = await ensure_archive(settings, project.slug)
        async with _archive_write_lock(archive):
            async with get_session() as session:
                existing_rows = await session.execute(
                    select(FileReservation, Agent.name)
                    .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                    .where(
                        cast(Any, FileReservation.project_id) == project_id,
                        cast(Any, FileReservation.released_ts).is_(None),
                        cast(Any, FileReservation.expires_ts) > _naive_utc(),
                    )
                )
                existing_reservations = [(row[0], row[1]) for row in existing_rows.all()]
            payloads: list[dict[str, Any]] = []
            ctx_branch: Optional[str] = None
            ctx_worktree: Optional[str] = None
            try:
                with _git_repo(project.human_key) as repo:
                    try:
                        ctx_branch = repo.active_branch.name
                    except Exception:
                        try:
                            ctx_branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
                        except Exception:
                            ctx_branch = None
                    try:
                        ctx_worktree = Path(repo.working_tree_dir or "").name or None
                    except Exception:
                        ctx_worktree = None
            except Exception:
                pass
            # Build union PathSpec for fast conflict pre-filtering (O(n+m) instead of O(n*m))
            union_spec = _build_reservation_union_spec(existing_reservations, agent.id, exclusive)

            # Pre-compute which paths might conflict using the union spec
            potentially_conflicting_paths: set[str] = set()
            if union_spec is not None:
                # Normalize paths for matching (same normalization as pattern matching)
                normalized_paths = [_normalize_pathspec_pattern(p) for p in paths]
                # Match all normalized paths against union in a single pass
                matching_normalized = set(union_spec.match_files(normalized_paths))
                # Build set of original paths that might conflict
                for orig_path, norm_path in zip(paths, normalized_paths, strict=True):
                    if norm_path in matching_normalized:
                        potentially_conflicting_paths.add(orig_path)
            else:
                # Fallback: all paths potentially conflict (PathSpec unavailable)
                potentially_conflicting_paths = set(paths)

            for path in paths:
                conflicting_holders: list[dict[str, Any]] = []

                # Fast path: skip detailed check if path cannot conflict with any reservation
                if path in potentially_conflicting_paths:
                    # Slow path: detailed attribution for potentially conflicting paths only
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
                file_reservation_payload = _file_reservation_payload(
                    project,
                    file_reservation,
                    agent,
                    branch=ctx_branch,
                    worktree=ctx_worktree,
                )
                payloads.append(file_reservation_payload)
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
            if payloads:
                await write_file_reservation_records(archive, payloads)
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
        format: Optional[str] = None,
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
                from rich.console import Console
                from rich.panel import Panel

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
            naive_now = _naive_utc(now)  # Compute once for consistency
            reservations: list[FileReservation] = []
            async with get_session() as session:
                select_stmt = (
                    select(FileReservation)
                    .where(
                        cast(Any, FileReservation.project_id) == project.id,
                        cast(Any, FileReservation.agent_id) == agent.id,
                        cast(Any, FileReservation.released_ts).is_(None),
                    )
                )
                if file_reservation_ids:
                    select_stmt = select_stmt.where(cast(Any, FileReservation.id).in_(file_reservation_ids))
                if paths:
                    select_stmt = select_stmt.where(cast(Any, FileReservation.path_pattern).in_(paths))
                result = await session.execute(select_stmt)
                reservations = list(result.scalars().all())
                if reservations:
                    ids = [res.id for res in reservations if res.id is not None]
                    if ids:
                        await session.execute(
                            update(FileReservation)
                            .where(
                                cast(Any, FileReservation.project_id) == project.id,
                                cast(Any, FileReservation.agent_id) == agent.id,
                                cast(Any, FileReservation.released_ts).is_(None),
                                cast(Any, FileReservation.id).in_(ids),
                            )
                            .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
                        )
                        await session.commit()
            affected = len(reservations)
            for reservation in reservations:
                reservation.released_ts = naive_now
            if reservations:
                await _write_file_reservation_records(
                    project,
                    [(reservation, agent) for reservation in reservations],
                )
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
        format: Optional[str] = None,
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
                .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                .where(
                    cast(Any, FileReservation.id) == file_reservation_id,
                    cast(Any, FileReservation.project_id) == project.id,
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
        naive_now = _naive_utc(now)
        async with get_session() as session:
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.id) == file_reservation_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()

        reservation.released_ts = naive_now
        await _write_file_reservation_records(
            project,
            [(reservation, holder)],
        )
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
                        "ctx": ctx,
                        "project_key": project_key,
                        "sender_name": agent_name,
                        "to": [holder.name],
                        "subject": f"[file-reservations] Released stale lock on {reservation.path_pattern}",
                        "body_md": "\n".join(body_lines),
                        "format": "json",
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
        format: Optional[str] = None,
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
                from rich.console import Console
                from rich.panel import Panel

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
                    cast(Any, FileReservation.project_id) == project.id,
                    cast(Any, FileReservation.agent_id) == agent.id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .order_by(asc(cast(Any, FileReservation.expires_ts)))
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
                # Convert to naive UTC for SQLite compatibility
                file_reservation.expires_ts = _naive_utc(base + timedelta(seconds=bump))
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
        await _write_file_reservation_records(
            project,
            [(reservation, agent) for reservation in file_reservations],
        )
        await ctx.info(f"Renewed {len(updated)} file_reservation(s) for '{agent.name}'.")
        return {"renewed": len(updated), "file_reservations": updated}

    # --- Build slots (coarse concurrency control) --------------------------------------------
    # Only registered when WORKTREES_ENABLED=1 to reduce token overhead for single-worktree setups

    if settings.worktrees_enabled:
        def _safe_component(value: str) -> str:
            # Keep it simple and dependency-free: replace common problematic filesystem chars
            safe = value.strip()
            for ch in ("/", "\\", ":", "*", "?", "\"", "<", ">", "|", " "):
                safe = safe.replace(ch, "_")
            return safe or "unknown"

        def _slot_dir(archive: ProjectArchive, slot: str) -> Path:
            safe = _safe_component(slot)
            return archive.root / "build_slots" / safe

        def _compute_branch(path: str) -> Optional[str]:
            try:
                with _git_repo(path) as repo:
                    try:
                        return repo.active_branch.name
                    except Exception:
                        return repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
            except Exception:
                return None

        def _read_active_slots(slot_path: Path, now: datetime) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            if not slot_path.exists():
                return results
            for f in slot_path.glob("*.json"):
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

        @mcp.tool(name="acquire_build_slot")
        @_instrument_tool("acquire_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def acquire_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            ttl_seconds: int = 3600,
            exclusive: bool = True,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Acquire a build slot (advisory), optionally exclusive. Returns conflicts when another holder is active.
            """
            project = await _get_project_by_identifier(project_key)
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            slot_path = _slot_dir(archive, slot)
            await asyncio.to_thread(slot_path.mkdir, parents=True, exist_ok=True)
            active = _read_active_slots(slot_path, now)

            branch = _compute_branch(project.human_key)
            holder_id = _safe_component(f"{agent_name}__{branch or 'unknown'}")
            lease_path = slot_path / f"{holder_id}.json"

            conflicts: list[dict[str, Any]] = []
            if exclusive:
                for entry in active:
                    if entry.get("agent") == agent_name and entry.get("branch") == branch:
                        continue
                    if entry.get("exclusive", True):
                        conflicts.append(entry)
            payload = {
                "slot": slot,
                "agent": agent_name,
                "branch": branch,
                "exclusive": exclusive,
                "acquired_ts": _iso(now),
                "expires_ts": _iso(now + timedelta(seconds=max(ttl_seconds, 60))),
            }
            with contextlib.suppress(Exception):
                await asyncio.to_thread(lease_path.write_text, json.dumps(payload, indent=2), "utf-8")
            if conflicts:
                await ctx.info(f"Build slot conflicts for '{slot}': {len(conflicts)}")
            return {"granted": payload, "conflicts": conflicts}

        @mcp.tool(name="renew_build_slot")
        @_instrument_tool("renew_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def renew_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            extend_seconds: int = 1800,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Extend expiry for an existing build slot lease. No-op if missing.
            """
            project = await _get_project_by_identifier(project_key)
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            slot_path = _slot_dir(archive, slot)
            branch = _compute_branch(project.human_key)
            holder_id = _safe_component(f"{agent_name}__{branch or 'unknown'}")
            lease_path = slot_path / f"{holder_id}.json"
            try:
                current = json.loads(lease_path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
            new_exp = _iso(now + timedelta(seconds=max(extend_seconds, 60)))
            current.update({"slot": slot, "agent": agent_name, "branch": branch, "expires_ts": new_exp})
            with contextlib.suppress(Exception):
                await asyncio.to_thread(lease_path.write_text, json.dumps(current, indent=2), "utf-8")
            return {"renewed": True, "expires_ts": new_exp}

        @mcp.tool(name="release_build_slot")
        @_instrument_tool("release_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def release_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Mark an active slot lease as released (non-destructive; keeps JSON with released_ts).
            """
            project = await _get_project_by_identifier(project_key)
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            slot_path = _slot_dir(archive, slot)
            branch = _compute_branch(project.human_key)
            holder_id = _safe_component(f"{agent_name}__{branch or 'unknown'}")
            lease_path = slot_path / f"{holder_id}.json"
            released = False
            try:
                data = {}
                if lease_path.exists():
                    data = json.loads(lease_path.read_text(encoding="utf-8"))
                data.update({"released_ts": _iso(now), "expires_ts": _iso(now)})
                await asyncio.to_thread(lease_path.write_text, json.dumps(data, indent=2), "utf-8")
                released = True
            except Exception:
                released = False
            return {"released": released, "released_at": _iso(now)}

    @mcp.resource("resource://config/environment{?format}", mime_type="application/json")
    def environment_resource(format: Optional[str] = None) -> dict[str, Any]:
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
        payload = {
            "environment": settings.environment,
            "database_url": settings.database.url,
            "http": {
                "host": settings.http.host,
                "port": settings.http.port,
                "path": settings.http.path,
            },
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://config/environment",
            format_value=format,
        )

    # --- Product Bus (Phase 2): ensure/link/search/resources ---------------------------------

    async def _get_product_by_key(session, key: str) -> Optional[Product]:
        # Key may match product_uid or name (case-sensitive by default)
        stmt = select(Product).where(cast(Any, (Product.product_uid == key) | (Product.name == key)))
        res = await session.execute(stmt)
        return res.scalars().first()

    if settings.worktrees_enabled:
        @mcp.tool(name="ensure_product")
        @_instrument_tool("ensure_product", cluster=CLUSTER_PRODUCT, capabilities={"product"})
        async def ensure_product_tool(
            ctx: Context,
            product_key: Optional[str] = None,
            name: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Ensure a Product exists. If not, create one.

            - product_key may be a product_uid or a name
            - If both are absent, error
            """
            await ensure_schema()
            key_raw = (product_key or name or "").strip()
            if not key_raw:
                raise ToolExecutionError("INVALID_ARGUMENT", "Provide product_key or name.")
            async with get_session() as session:
                prod = await _get_product_by_key(session, key_raw)
                if prod is None:
                    # Create with strict uid pattern; otherwise generate uid and normalize name
                    import uuid as _uuid
                    import re as _re
                    uid_pattern = _re.compile(r"^[A-Fa-f0-9]{8,64}$")
                    if product_key and uid_pattern.fullmatch(product_key.strip()):
                        uid = product_key.strip().lower()
                    else:
                        uid = _uuid.uuid4().hex[:20]
                    display_name = (name or key_raw).strip()
                    # Collapse internal whitespace and cap length
                    display_name = " ".join(display_name.split())[:255] or uid
                    prod = Product(product_uid=uid, name=display_name)
                    session.add(prod)
                    await session.commit()
                    await session.refresh(prod)
            return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": _iso(prod.created_at)}
    else:
        async def ensure_product_tool(
            ctx: Context,
            product_key: Optional[str] = None,
            name: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="products_link")
        @_instrument_tool("products_link", cluster=CLUSTER_PRODUCT, capabilities={"product"}, project_arg="project_key")
        async def products_link_tool(
            ctx: Context,
            product_key: str,
            project_key: str,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Link a project into a product (idempotent).
            """
            await ensure_schema()
            async with get_session() as session:
                prod = await _get_product_by_key(session, product_key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
                # Resolve project
                project = await _get_project_by_identifier(project_key)
                if project.id is None:
                    raise ToolExecutionError("NOT_FOUND", f"Project '{project_key}' not found.", recoverable=True)
                # Link if missing
                existing = await session.execute(
                    select(ProductProjectLink).where(
                        cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id),
                        cast(Any, ProductProjectLink.project_id) == cast(Any, project.id),
                    )
                )
                link = existing.scalars().first()
                if link is None:
                    link = ProductProjectLink(product_id=int(cast(int, prod.id)), project_id=int(project.id))
                    session.add(link)
                    await session.commit()
                    await session.refresh(link)
                return {
                    "product": {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name},
                    "project": {"id": project.id, "slug": project.slug, "human_key": project.human_key},
                    "linked": True,
                }
    else:
        async def products_link_tool(
            ctx: Context,
            product_key: str,
            project_key: str,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.resource("resource://product/{key}{?format}", mime_type="application/json")
        def product_resource(key: str, format: Optional[str] = None) -> dict[str, Any]:
            """
            Inspect product and list linked projects.
            """
            key, query_params = _split_slug_and_query(key)
            format_value = format or query_params.get("format")
            # Safe runner that works even if an event loop is already running
            def _run_coro_sync(coro):
                try:
                    asyncio.get_running_loop()
                    # Run in a separate thread to avoid nested loop issues
                except RuntimeError:
                    return asyncio.run(coro)
                import threading
                import queue
                q: "queue.Queue[tuple[bool, Any]]" = queue.Queue()
                def _runner():
                    try:
                        q.put((True, asyncio.run(coro)))
                    except Exception as e:
                        q.put((False, e))
                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                ok, val = q.get()
                if ok:
                    return val
                raise val
            async def _load() -> dict[str, Any]:
                await ensure_schema()
                async with get_session() as session:
                    prod = await _get_product_by_key(session, key.strip())
                    if prod is None:
                        raise ToolExecutionError("NOT_FOUND", f"Product '{key}' not found.", recoverable=True)
                    proj_rows = await session.execute(
                        select(Project).join(ProductProjectLink, cast(Any, ProductProjectLink.project_id) == Project.id).where(
                            cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id)
                        )
                    )
                    projects = [
                        {"id": p.id, "slug": p.slug, "human_key": p.human_key, "created_at": _iso(p.created_at)}
                        for p in proj_rows.scalars().all()
                    ]
                    return {
                        "id": prod.id,
                        "product_uid": prod.product_uid,
                        "name": prod.name,
                        "created_at": _iso(prod.created_at),
                        "projects": projects,
                    }
            # Run async in a synchronous resource
            payload = _run_coro_sync(_load())
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name="resource://product/{key}",
                format_value=format_value,
            )

    if settings.worktrees_enabled:
        @mcp.tool(name="search_messages_product")
        @_instrument_tool("search_messages_product", cluster=CLUSTER_PRODUCT, capabilities={"search"})
        async def search_messages_product(
            ctx: Context,
            product_key: str,
            query: str,
            limit: int = 20,
            format: Optional[str] = None,
        ) -> Any:
            """
            Full-text search across all projects linked to a product.
            """
            # Sanitize the FTS query first
            sanitized_query = _sanitize_fts_query(query)
            if sanitized_query is None:
                await ctx.info(f"Search query '{query}' is not searchable, returning empty results.")
                try:
                    from fastmcp.tools.tool import ToolResult
                    return ToolResult(structured_content={"result": []})
                except Exception:
                    return []

            await ensure_schema()
            rows: list[Any] = []
            async with get_session() as session:
                prod = await _get_product_by_key(session, product_key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
                proj_ids_rows = await session.execute(
                    select(ProductProjectLink.project_id).where(cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id))
                )
                proj_ids = [int(row[0]) for row in proj_ids_rows.fetchall()]
                if not proj_ids:
                    return []
                # FTS search limited to projects in proj_ids
                try:
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
                        {"proj_ids": proj_ids, "query": sanitized_query, "limit": limit},
                    )
                    rows = list(result.mappings().all())
                except Exception as fts_err:
                    logger.warning("FTS product query failed, returning empty results", extra={"query": sanitized_query, "error": str(fts_err)})
                    fallback_terms = _extract_like_terms(query)
                    if not fallback_terms:
                        rows = []
                    else:
                        clauses = []
                        params: dict[str, Any] = {"proj_ids": proj_ids, "limit": limit}
                        for idx, term in enumerate(fallback_terms):
                            key = f"t{idx}"
                            params[key] = f"%{_like_escape(term)}%"
                            clauses.append(
                                f"(m.subject LIKE :{key} ESCAPE '\\\\' OR m.body_md LIKE :{key} ESCAPE '\\\\')"
                            )
                        where_clause = " AND ".join(clauses)
                        result = await session.execute(
                            text(
                                f"""
                                SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                                       m.thread_id, a.name AS sender_name, m.project_id
                                FROM messages m
                                JOIN agents a ON m.sender_id = a.id
                                WHERE m.project_id IN :proj_ids AND {where_clause}
                                ORDER BY m.created_ts DESC
                                LIMIT :limit
                                """
                            ).bindparams(bindparam("proj_ids", expanding=True)),
                            params,
                        )
                        rows = list(result.mappings().all())
            items = [
                {
                    "id": row["id"],
                    "subject": row["subject"],
                    "importance": row["importance"],
                    "ack_required": row["ack_required"],
                    "created_ts": _iso(row["created_ts"]),
                    "thread_id": row["thread_id"],
                    "from": row["sender_name"],
                    "project_id": row["project_id"],
                }
                for row in rows
            ]
            try:
                from fastmcp.tools.tool import ToolResult
                return ToolResult(structured_content={"result": items})
            except Exception:
                return items
    else:
        async def search_messages_product(
            ctx: Context,
            product_key: str,
            query: str,
            limit: int = 20,
            format: Optional[str] = None,
        ) -> Any:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="fetch_inbox_product")
        @_instrument_tool("fetch_inbox_product", cluster=CLUSTER_PRODUCT, capabilities={"messaging", "read"})
        async def fetch_inbox_product(
            ctx: Context,
            product_key: str,
            agent_name: str,
            limit: int = 20,
            urgent_only: bool = False,
            include_bodies: bool = False,
            since_ts: Optional[str] = None,
            format: Optional[str] = None,
        ) -> list[dict[str, Any]]:
            """
            Retrieve recent messages for an agent across all projects linked to a product (non-mutating).
            """
            await ensure_schema()
            # Collect linked projects
            async with get_session() as session:
                prod = await _get_product_by_key(session, product_key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
                proj_rows = await session.execute(
                    select(Project).join(ProductProjectLink, cast(Any, ProductProjectLink.project_id) == Project.id).where(
                        cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id)
                    )
                )
                projects: list[Project] = list(proj_rows.scalars().all())
            # For each project, if agent exists, list inbox items
            messages: list[dict[str, Any]] = []
            for project in projects:
                try:
                    ag = await _get_agent(project, agent_name)
                except Exception:
                    continue
                proj_items = await _list_inbox(project, ag, limit, urgent_only, include_bodies, since_ts)
                for item in proj_items:
                    item["project_id"] = item.get("project_id") or project.id
                    messages.append(item)
            # Sort by created_ts desc and trim to limit
            def _dt_key(it: dict[str, Any]) -> float:
                ts = _parse_iso(str(it.get("created_ts") or ""))
                return ts.timestamp() if ts else 0.0
            messages.sort(key=_dt_key, reverse=True)
            return messages[: max(0, int(limit))]
    else:
        async def fetch_inbox_product(
            ctx: Context,
            product_key: str,
            agent_name: str,
            limit: int = 20,
            urgent_only: bool = False,
            include_bodies: bool = False,
            since_ts: Optional[str] = None,
            format: Optional[str] = None,
        ) -> list[dict[str, Any]]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="summarize_thread_product")
        @_instrument_tool("summarize_thread_product", cluster=CLUSTER_PRODUCT, capabilities={"summarization", "search"})
        async def summarize_thread_product(
            ctx: Context,
            product_key: str,
            thread_id: str,
            include_examples: bool = False,
            llm_mode: bool = True,
            llm_model: Optional[str] = None,
            per_thread_limit: Optional[int] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Summarize a thread (by id or thread key) across all projects linked to a product.
            """
            await ensure_schema()
            sender_alias = aliased(Agent)
            try:
                seed_id = int(thread_id)
            except ValueError:
                seed_id = None
            criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
            if seed_id is not None:
                criteria.append(cast(Any, Message.id) == seed_id)

            async with get_session() as session:
                prod = await _get_product_by_key(session, product_key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
                proj_ids_rows = await session.execute(
                    select(ProductProjectLink.project_id).where(cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id))
                )
                proj_ids = [int(row[0]) for row in proj_ids_rows.fetchall()]
                if not proj_ids:
                    return {"thread_id": thread_id, "summary": {"participants": [], "key_points": [], "action_items": [], "total_messages": 0}, "examples": []}
                stmt = (
                    select(Message, sender_alias.name)
                    .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                    .where(cast(Any, Message.project_id).in_(proj_ids), or_(*cast(Any, criteria)))
                    .order_by(asc(cast(Any, Message.created_ts)))
                )
                if per_thread_limit:
                    stmt = stmt.limit(per_thread_limit)
                raw_rows = (await session.execute(stmt)).all()
            rows = [(row[0], row[1]) for row in raw_rows]
            summary = _summarize_messages(rows)
            heuristic_key_points = list(summary.get("key_points", []))

            # Optional LLM refinement (same as project-level)
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
                            if heuristic_key_points and isinstance(summary.get("key_points"), list):
                                keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")
                                extra = [
                                    kp for kp in heuristic_key_points
                                    if any(token in str(kp).upper() for token in keywords)
                                ]
                                if extra:
                                    merged: list[str] = []
                                    for item in summary["key_points"] + extra:
                                        if item not in merged:
                                            merged.append(item)
                                    summary["key_points"] = merged[:10]
                except Exception as e:
                    await ctx.debug(f"summarize_thread_product.llm_skipped: {e}")

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
            await ctx.info(f"Summarized thread '{thread_id}' across product '{product_key}' with {len(rows)} messages")
            return {"thread_id": thread_id, "summary": summary, "examples": examples}
    else:
        async def summarize_thread_product(
            ctx: Context,
            product_key: str,
            thread_id: str,
            include_examples: bool = False,
            llm_mode: bool = True,
            llm_model: Optional[str] = None,
            per_thread_limit: Optional[int] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")
    if settings.worktrees_enabled:
        @mcp.resource("resource://identity/{project}{?format}", mime_type="application/json")
        def identity_resource(project: str, format: Optional[str] = None) -> dict[str, Any]:
            """
            Inspect identity resolution for a given project path. Returns the slug actually used,
            the identity mode in effect, canonical path for the selected mode, and git repo facts.
            """
            raw_path, query_params = _split_slug_and_query(project)
            format_value = format or query_params.get("format")
            target_path = str(Path(raw_path).expanduser().resolve())
            payload = _resolve_project_identity(target_path)
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name="resource://identity/{project}",
                format_value=format_value,
            )
    @mcp.resource("resource://tooling/directory{?format}", mime_type="application/json")
    def tooling_directory_resource(format: Optional[str] = None) -> dict[str, Any]:
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
                        "summary": "Extract participants, key points, and action items for one or more threads.",
                        "use_when": "Briefing new agents on long discussions, closing loops, or producing digests.",
                        "related": ["search_messages"],
                        "expected_frequency": "When threads exceed quick skim length or at cadence checkpoints.",
                        "required_capabilities": ["search", "summarization"],
                        "usage_examples": [
                            {"hint": "Single thread", "sample": "summarize_thread(project_key='backend', thread_id='TKT-123', include_examples=True)"},
                            {"hint": "Multi-thread digest", "sample": "summarize_thread(project_key='backend', thread_id='TKT-123,UX-42,BUG-99')"},
                        ],
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

        default_format = settings.output_format_default or settings.toon_default_format or "json"
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "metrics_uri": "resource://tooling/metrics",
            "output_formats": {
                "default": default_format,
                "tool_param": "format",
                "resource_query": "format",
                "values": ["json", "toon"],
                "toon_envelope": {"format": "toon", "data": "<TOON>", "meta": {"requested": "toon"}},
            },
            "clusters": clusters,
            "playbooks": playbooks,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/directory",
            format_value=format,
        )

    @mcp.resource("resource://tooling/schemas{?format}", mime_type="application/json")
    def tooling_schemas_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Expose JSON-like parameter schemas for tools/macros to prevent drift.

        This is a lightweight, hand-maintained view focusing on the most error-prone
        parameters and accepted aliases to guide clients.
        """
        default_format = settings.output_format_default or settings.toon_default_format or "json"
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "global_optional": ["format"],
            "output_formats": {
                "default": default_format,
                "tool_param": "format",
                "resource_query": "format",
                "values": ["json", "toon"],
                "toon_envelope": {"format": "toon", "data": "<TOON>", "meta": {"requested": "toon"}},
            },
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
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/schemas",
            format_value=format,
        )

    @mcp.resource("resource://tooling/metrics{?format}", mime_type="application/json")
    def tooling_metrics_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Expose aggregated tool call/error counts for analysis."""
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "tools": _tool_metrics_snapshot(),
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/metrics",
            format_value=format,
        )

    @mcp.resource("resource://tooling/locks{?format}", mime_type="application/json")
    def tooling_locks_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Return lock metadata from the shared archive storage."""

        settings_local = get_settings()
        payload = collect_lock_status(settings_local)
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/locks",
            format_value=format,
        )

    @mcp.resource("resource://tooling/capabilities/{agent}{?project,format}", mime_type="application/json")
    def tooling_capabilities_resource(
        agent: str,
        project: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        # Parse query embedded in agent path if present (robust to FastMCP variants)
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        caps = _capabilities_for(agent, project)
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "agent": agent,
            "project": project,
            "capabilities": caps,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/capabilities/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://tooling/recent/{window_seconds}{?agent,project,format}", mime_type="application/json")
    def tooling_recent_resource(
        window_seconds: str,
        agent: Optional[str] = None,
        project: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        # Allow query string to be embedded in the path segment per some transports
        format_value = format
        if "?" in window_seconds:
            seg, _, qs = window_seconds.partition("?")
            window_seconds = seg
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                agent = agent or (parsed.get("agent") or [None])[0]
                project = project or (parsed.get("project") or [None])[0]
                format_value = format_value or _extract_format_param(parsed)
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
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "window_seconds": win,
            "count": len(entries),
            "entries": entries,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/recent/{window_seconds}",
            format_value=format_value,
        )

    @mcp.resource("resource://projects{?format}", mime_type="application/json")
    async def projects_resource(format: Optional[str] = None) -> list[dict[str, Any]]:
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
            result = await session.execute(select(Project).order_by(asc(cast(Any, Project.created_at))))
            projects = result.scalars().all()
            def _is_ignored(name: str) -> bool:
                return any(_fnmatch.fnmatch(name, pat) for pat in ignore_patterns)
            filtered = [p for p in projects if not (_is_ignored(p.slug) or _is_ignored(p.human_key))]
            payload = [_project_to_dict(project) for project in filtered]
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name="resource://projects",
                format_value=format,
            )

    @mcp.resource("resource://project/{slug}{?format}", mime_type="application/json")
    async def project_detail(slug: str, format: Optional[str] = None) -> dict[str, Any]:
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
        slug_value, query_params = _split_slug_and_query(slug)
        format_value = format or query_params.get("format")
        project = await _get_project_by_identifier(slug_value)
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(select(Agent).where(cast(Any, Agent.project_id == project.id)))
            agents = result.scalars().all()
        payload = {
            **_project_to_dict(project),
            "agents": [_agent_to_dict(agent) for agent in agents],
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://project/{slug}",
            format_value=format_value,
        )

    @mcp.resource("resource://agents/{project_key}{?format}", mime_type="application/json")
    async def agents_directory(project_key: str, format: Optional[str] = None) -> dict[str, Any]:
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
        key_value, query_params = _split_slug_and_query(project_key)
        format_value = format or query_params.get("format")
        project = await _get_project_by_identifier(key_value)
        await ensure_schema()

        async with get_session() as session:
            # Get all agents in the project
            result = await session.execute(
                select(Agent).where(cast(Any, Agent.project_id == project.id)).order_by(desc(cast(Any, Agent.last_active_ts)))
            )
            agents = result.scalars().all()

            # Get unread message counts for all agents in one query
            unread_counts_stmt = (
                select(
                    MessageRecipient.agent_id,
                    func.count(cast(Any, MessageRecipient.message_id)).label("unread_count"),
                )
                .where(
                    cast(Any, MessageRecipient.read_ts).is_(None),
                    cast(Any, MessageRecipient.agent_id).in_([agent.id for agent in agents]),
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

        payload = {
            "project": {
                "slug": project.slug,
                "human_key": project.human_key,
            },
            "agents": agent_data,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://agents/{project_key}",
            format_value=format_value,
        )

    @mcp.resource("resource://file_reservations/{slug}{?active_only,format}", mime_type="application/json")
    async def file_reservations_resource(
        slug: str,
        active_only: bool = False,
        format: Optional[str] = None,
    ) -> list[dict[str, Any]]:
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
        format_value = format or query_params.get("format")
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
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://file_reservations/{slug}",
            format_value=format_value,
        )

    @mcp.resource("resource://message/{message_id}{?project,format}", mime_type="application/json")
    async def message_resource(
        message_id: str,
        project: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
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
        format_value = format
        if "?" in message_id:
            id_part, _, qs = message_id.partition("?")
            message_id = id_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        if project is None:
            # Try to infer project by message id when unique
            async with get_session() as s_auto:
                rows = await s_auto.execute(select(Project, Message).join(Message, cast(Any, Message.project_id) == Project.id).where(cast(Any, Message.id) == int(message_id)).limit(2))
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
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://message/{message_id}",
            format_value=format_value,
        )

    @mcp.resource("resource://thread/{thread_id}{?project,include_bodies,format}", mime_type="application/json")
    async def thread_resource(
        thread_id: str,
        project: Optional[str] = None,
        include_bodies: bool = False,
        format: Optional[str] = None,
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
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
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
                        .join(Message, cast(Any, Message.project_id) == Project.id)
                        .where(cast(Any, Message.id) == msg_id)
                        .limit(2)
                    )
                    projects = [row[0] for row in rows.all()]
                else:
                    rows = await s_auto.execute(
                        select(Project)
                        .join(Message, cast(Any, Message.project_id) == Project.id)
                        .where(cast(Any, Message.thread_id == thread_id))
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
                .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                .where(cast(Any, Message.project_id == project_obj.id), or_(*cast(Any, criteria)))
                .order_by(asc(cast(Any, Message.created_ts)))
            )
            result = await session.execute(stmt)
            rows = result.all()
        messages = []
        for message, sender_name in rows:
            payload = _message_to_dict(message, include_body=include_bodies)
            payload["from"] = sender_name
            messages.append(payload)
        payload = {"project": project_obj.human_key, "thread_id": thread_id, "messages": messages}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://thread/{thread_id}",
            format_value=format_value,
        )

    @mcp.resource(
        "resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit,format}",
        mime_type="application/json",
    )
    async def inbox_resource(
        agent: str,
        project: Optional[str] = None,
        since_ts: Optional[str] = None,
        urgent_only: bool = False,
        include_bodies: bool = False,
        limit: int = 20,
        format: Optional[str] = None,
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
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            # Auto-detect project by agent name if uniquely identifiable
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
        payload = {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "count": len(enriched),
            "messages": enriched,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://inbox/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/urgent-unread/{agent}{?project,limit,format}", mime_type="application/json")
    async def urgent_unread_view(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
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
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
                        cast(Any, MessageRecipient.message_id == item["id"]), cast(Any, MessageRecipient.agent_id == agent_obj.id)
                    )
                )
                read_ts = result.scalar_one_or_none()
                if read_ts is None:
                    unread.append(item)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(unread), "messages": unread[:limit]}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/urgent-unread/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/ack-required/{agent}{?project,limit,format}", mime_type="application/json")
    async def ack_required_view(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
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
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(desc(cast(Any, Message.created_ts)))
                .limit(limit)
            )
            for msg, kind in rows.all():
                payload = _message_to_dict(msg, include_body=False)
                payload["kind"] = kind
                out.append(payload)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/ack-required/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/acks-stale/{agent}{?project,ttl_seconds,limit,format}", mime_type="application/json")
    async def acks_stale_view(
        agent: str,
        project: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        limit: int = 20,
        format: Optional[str] = None,
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
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(cast(Any, Message.created_ts)))
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
        payload = {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "ttl_seconds": ttl,
            "count": len(out),
            "messages": out,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/acks-stale/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/ack-overdue/{agent}{?project,ttl_minutes,limit,format}", mime_type="application/json")
    async def ack_overdue_view(
        agent: str,
        project: Optional[str] = None,
        ttl_minutes: int = 60,
        limit: int = 50,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List messages requiring acknowledgement older than ttl_minutes without ack."""
        # Parse query embedded in agent path if present
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(cast(Any, Message.created_ts)))
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
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/ack-overdue/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://mailbox/{agent}{?project,limit,format}", mime_type="application/json")
    async def mailbox_resource(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        List recent messages in an agent's mailbox with lightweight Git commit context.

        Returns
        -------
        dict
            { project, agent, count, messages: [{ id, subject, from, created_ts, importance, ack_required, kind, commit: {hexsha, summary} | null }] }
        """
        # Parse query embedded in agent path if present
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://mailbox/{agent}",
            format_value=format_value,
        )

    @mcp.resource(
        "resource://mailbox-with-commits/{agent}{?project,limit,format}",
        mime_type="application/json",
    )
    async def mailbox_with_commits_resource(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List recent messages in an agent's mailbox with commit metadata including diff summaries."""
        # Parse query embedded in agent path if present
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        if project is None:
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
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
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://mailbox-with-commits/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://outbox/{agent}{?project,limit,include_bodies,since_ts,format}", mime_type="application/json")
    async def outbox_resource(
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        include_bodies: bool = False,
        since_ts: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List messages sent by the agent, enriched with commit metadata for canonical files."""
        # Support toolkits that incorrectly pass query in the template segment
        format_value = format
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
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        if project is None:
            # Auto-detect project by agent name if uniquely identifiable
            async with get_session() as s_auto:
                rows = await s_auto.execute(
                    select(Project)
                    .join(Agent, cast(Any, Agent.project_id) == Project.id)
                    .where(func.lower(Agent.name) == agent.lower())
                    .limit(2)
                )
                projects = [row[0] for row in rows.all()]
            if len(projects) == 1:
                project_obj = projects[0]
            else:
                raise ValueError("project parameter is required for outbox resource")
        else:
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
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://outbox/{agent}",
            format_value=format_value,
        )

    # No explicit output-schema transform; the tool returns ToolResult with {"result": ...}

    # -------------------------------------------------------------------------------------------------
    # Tool Filtering: Remove tools that shouldn't be exposed based on settings
    # -------------------------------------------------------------------------------------------------
    if settings.tool_filter.enabled:
        _apply_tool_filter(mcp, settings)

    return mcp


def _apply_tool_filter(mcp: FastMCP, settings: Settings) -> None:
    """Remove filtered tools from the MCP server's tool registry.

    This is a post-registration step that removes tools that shouldn't be exposed
    based on the tool filter settings. This approach is cleaner than conditional
    registration because it doesn't require modifying every @mcp.tool decorator.
    """
    # FastMCP stores tools in _tool_manager._tools (dict keyed by tool name)
    tool_manager = getattr(cast(_FastMCPToolManagerLike, mcp), "_tool_manager", None)
    if tool_manager is None:
        logger.warning("Tool filtering enabled but FastMCP tool manager not found")
        return

    tools_registry = getattr(cast(_ToolRegistryLike, tool_manager), "_tools", None)
    if tools_registry is None or not isinstance(tools_registry, dict):
        logger.warning("Tool filtering enabled but tool registry not accessible")
        return

    # Identify tools to remove
    to_remove: list[str] = []
    for tool_name in list(tools_registry.keys()):
        cluster = TOOL_CLUSTER_MAP.get(tool_name, "unclassified")
        if not _should_expose_tool(tool_name, cluster, settings):
            to_remove.append(tool_name)
            _FILTERED_TOOLS.add(tool_name)

    # Remove filtered tools
    for tool_name in to_remove:
        del tools_registry[tool_name]
        # Also remove from metadata registries
        TOOL_CLUSTER_MAP.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)

    if to_remove:
        profile = settings.tool_filter.profile
        logger.info(
            f"Tool filtering active (profile={profile}): removed {len(to_remove)} tools, "
            f"{len(tools_registry)} tools exposed"
        )
