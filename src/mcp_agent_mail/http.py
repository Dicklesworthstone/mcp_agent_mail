"""HTTP transport helpers wrapping FastMCP with FastAPI."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import importlib
import json
import logging
from typing import Any, cast

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import Receive, Scope, Send

from .app import _expire_stale_claims, _tool_metrics_snapshot, build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .storage import AsyncFileLock, ensure_archive, write_agent_profile, write_claim_record


async def _project_slug_from_id(pid: int | None) -> str | None:
    if pid is None:
        return None
    async with get_session() as session:
        row = await session.execute(text("SELECT slug FROM projects WHERE id = :pid"), {"pid": pid})
        res = row.fetchone()
        return res[0] if res and res[0] else None

__all__ = ["build_http_app", "main"]


def _decode_jwt_header_segment(token: str) -> dict[str, object] | None:
    """Return decoded JWT header without verifying signature."""
    try:
        segment = token.split(".", 1)[0]
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None

_LOGGING_CONFIGURED = False


def _configure_logging(settings: Settings) -> None:
    """Initialize structlog and stdlib logging formatting."""
    # Idempotent setup
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
    ]
    if settings.log_json_enabled:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.processors.KeyValueRenderer(key_order=["event", "path", "status"]))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    # mark configured
    _LOGGING_CONFIGURED = True



class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":  # allow CORS preflight
            return await call_next(request)
        if request.url.path.startswith("/health/"):
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {self._token}":
            return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return await call_next(request)


class SecurityAndRateLimitMiddleware(BaseHTTPMiddleware):
    """JWT auth (optional), RBAC, and token-bucket rate limiting.

    - If JWT is enabled, validates Authorization: Bearer <token> using either HMAC secret or JWKS URL.
    - Enforces basic RBAC when enabled: read-only roles may only call whitelisted tools and resource reads.
    - Applies per-endpoint token-bucket limits (tools vs resources) with in-memory or Redis backend.
    """

    def __init__(self, app: FastAPI, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self._jwt_enabled = bool(getattr(settings.http, "jwt_enabled", False))
        self._rbac_enabled = bool(getattr(settings.http, "rbac_enabled", True))
        self._reader_roles = set(getattr(settings.http, "rbac_reader_roles", []) or [])
        self._writer_roles = set(getattr(settings.http, "rbac_writer_roles", []) or [])
        self._readonly_tools = set(getattr(settings.http, "rbac_readonly_tools", []) or [])
        self._default_role = getattr(settings.http, "rbac_default_role", "tools")
        # Token bucket state (memory)
        from time import monotonic

        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}
        # Redis client (optional)
        self._redis = None
        if (
            getattr(settings.http, "rate_limit_backend", "memory") == "redis"
            and getattr(settings.http, "rate_limit_redis_url", "")
        ):
            try:
                redis_asyncio = importlib.import_module("redis.asyncio")
                Redis = redis_asyncio.Redis
                self._redis = Redis.from_url(settings.http.rate_limit_redis_url)
            except Exception:
                self._redis = None

    async def _decode_jwt(self, token: str) -> dict | None:
        """Validate and decode JWT, returning claims or None on failure."""
        with contextlib.suppress(Exception):
            jose_mod = importlib.import_module("authlib.jose")
            JsonWebKey = jose_mod.JsonWebKey
            JsonWebToken = jose_mod.JsonWebToken
            algs = list(getattr(self.settings.http, "jwt_algorithms", ["HS256"]))
            jwt = JsonWebToken(algs)
            audience = getattr(self.settings.http, "jwt_audience", None) or None
            issuer = getattr(self.settings.http, "jwt_issuer", None) or None
            jwks_url = getattr(self.settings.http, "jwt_jwks_url", None) or None
            secret = getattr(self.settings.http, "jwt_secret", None) or None

            header = _decode_jwt_header_segment(token)
            if header is None:
                return None
            key = None
            if jwks_url:
                with contextlib.suppress(Exception):
                    httpx = importlib.import_module("httpx")
                    AsyncClient = httpx.AsyncClient
                    async with AsyncClient(timeout=5) as client:
                        jwks = (await client.get(jwks_url)).json()
                    key_set = JsonWebKey.import_key_set(jwks)
                    kid = header.get("kid")
                    key = key_set.find_by_kid(kid) if kid else key_set.keys[0]
            elif secret:
                with contextlib.suppress(Exception):
                    key = JsonWebKey.import_key(secret, {'kty': 'oct'})
            if key is None:
                return None
            with contextlib.suppress(Exception):
                claims = jwt.decode(token, key)
                if audience:
                    claims.validate_aud(audience)
                if issuer and str(claims.get('iss') or '') != issuer:
                    return None
                claims.validate()
                return dict(claims)
        return None

    @staticmethod
    def _classify_request(path: str, method: str, body_bytes: bytes) -> tuple[str, str | None]:
        """Return (kind, tool_name) where kind is 'tools'|'resources'|'other'."""
        if method.upper() != "POST":
            return "other", None
        if not body_bytes:
            return "other", None
        with contextlib.suppress(Exception):
            import json as _json
            payload = _json.loads(body_bytes)
            rpc_method = str(payload.get("method", ""))
            if rpc_method == "tools/call":
                params = payload.get("params", {}) or {}
                tool_name = params.get("name")
                return "tools", tool_name if isinstance(tool_name, str) else None
            if rpc_method == "resources/read":
                return "resources", None
            return "other", None
        return "other", None

    def _rate_limits_for(self, kind: str) -> tuple[int, int]:
        # return (per_minute, burst)
        if kind == "tools":
            rpm = int(getattr(self.settings.http, "rate_limit_tools_per_minute", 60) or 60)
            burst = int(getattr(self.settings.http, "rate_limit_tools_burst", 0) or 0)
        elif kind == "resources":
            rpm = int(getattr(self.settings.http, "rate_limit_resources_per_minute", 120) or 120)
            burst = int(getattr(self.settings.http, "rate_limit_resources_burst", 0) or 0)
        else:
            rpm = int(getattr(self.settings.http, "rate_limit_per_minute", 60) or 60)
            burst = 0
        burst = int(burst) if burst > 0 else max(1, rpm)
        return rpm, burst

    async def _consume_bucket(self, key: str, per_minute: int, burst: int) -> bool:
        """Return True if token granted, False if limited."""
        if per_minute <= 0:
            return True
        rate_per_sec = per_minute / 60.0
        now = self._monotonic()

        # Redis backend
        if self._redis is not None:
            try:
                lua = (
                    "local key = KEYS[1]\n"
                    "local now = tonumber(ARGV[1])\n"
                    "local rate = tonumber(ARGV[2])\n"
                    "local burst = tonumber(ARGV[3])\n"
                    "local state = redis.call('HMGET', key, 'tokens', 'ts')\n"
                    "local tokens = tonumber(state[1]) or burst\n"
                    "local ts = tonumber(state[2]) or now\n"
                    "local delta = now - ts\n"
                    "tokens = math.min(burst, tokens + delta * rate)\n"
                    "local allowed = 0\n"
                    "if tokens >= 1 then tokens = tokens - 1 allowed = 1 end\n"
                    "redis.call('HMSET', key, 'tokens', tokens, 'ts', now)\n"
                    "redis.call('EXPIRE', key, math.ceil(burst / math.max(rate, 0.001)))\n"
                    "return allowed\n"
                )
                allowed = await self._redis.eval(lua, 1, f"rl:{key}", now, rate_per_sec, burst)
                return bool(int(allowed or 0) == 1)
            except Exception:
                # Fallback to memory on Redis failure
                pass

        # In-memory token bucket
        tokens, ts = self._buckets.get(key, (float(burst), now))
        elapsed = max(0.0, now - ts)
        tokens = min(float(burst), tokens + elapsed * rate_per_sec)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        tokens -= 1.0
        self._buckets[key] = (tokens, now)
        return True

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        # Allow CORS preflight and health endpoints
        if request.method == "OPTIONS" or request.url.path.startswith("/health/"):
            return await call_next(request)

        # Read body once and restore for downstream
        try:
            body_bytes = await request.body()
            async def _receive() -> dict:
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            cast(Any, request)._receive = _receive
        except Exception:
            body_bytes = b""

        kind, tool_name = self._classify_request(request.url.path, request.method, body_bytes)

        # JWT auth (if enabled)
        if self._jwt_enabled:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
            token = auth_header.split(" ", 1)[1].strip()
            claims_dict = await self._decode_jwt(token)
            if claims_dict is None:
                return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
            claims = cast(dict[str, Any], claims_dict)
            request.state.jwt_claims = claims
            roles_raw = claims.get(self.settings.http.jwt_role_claim, [])
            if isinstance(roles_raw, str):
                roles = {roles_raw}
            elif isinstance(roles_raw, (list, tuple)):
                roles = {str(r) for r in roles_raw}
            else:
                roles = set()
            if not roles:
                roles = {self._default_role}
        else:
            roles = {self._default_role}

        # RBAC enforcement
        if self._rbac_enabled and kind in {"tools", "resources"}:
            is_reader = bool(roles & self._reader_roles)
            is_writer = bool(roles & self._writer_roles) or (not roles)
            if kind == "resources":
                pass  # readers allowed
            elif kind == "tools":
                if not tool_name:
                    # Without name, assume write-required to be safe
                    if not is_writer:
                        return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                else:
                    if tool_name in self._readonly_tools:
                        if not is_reader and not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                    else:
                        if not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

        # Rate limiting
        if self.settings.http.rate_limit_enabled:
            rpm, burst = self._rate_limits_for(kind)
            identity = (request.client.host if request.client else "ip-unknown")
            # Prefer stable subject from JWT if present
            with contextlib.suppress(Exception):
                maybe_claims = getattr(request.state, "jwt_claims", None)
                if isinstance(maybe_claims, dict):
                    sub = maybe_claims.get("sub")
                if isinstance(sub, str) and sub:
                    identity = f"sub:{sub}"
            endpoint = tool_name or "*"
            key = f"{kind}:{endpoint}:{identity}"
            allowed = await self._consume_bucket(key, rpm, burst)
            if not allowed:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

        return await call_next(request)


async def readiness_check() -> None:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(text("SELECT 1"))


def build_http_app(settings: Settings, server=None) -> FastAPI:
    # Configure logging once
    _configure_logging(settings)
    if server is None:
        server = build_mcp_server()

    # Build MCP HTTP sub-app with stateless mode for ASGI test transports
    mcp_http_app = server.http_app(path="/", stateless_http=True, json_response=True)

    # no-op wrapper removed; using explicit stateless adapter below

    # Background workers lifecycle
    async def _startup() -> None:  # pragma: no cover - service lifecycle
        if not (settings.claims_cleanup_enabled or settings.ack_ttl_enabled or settings.retention_report_enabled or settings.quota_enabled or settings.tool_metrics_emit_enabled):
            fastapi_app.state._background_tasks = []
            return
        async def _worker_cleanup() -> None:
            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        rows = await session.execute(text("SELECT DISTINCT project_id FROM claims"))
                        pids = [r[0] for r in rows.fetchall() if r[0] is not None]
                    for pid in pids:
                        with contextlib.suppress(Exception):
                            await _expire_stale_claims(pid)
                    try:
                        rich_console = importlib.import_module("rich.console")
                        rich_panel = importlib.import_module("rich.panel")
                        Console = rich_console.Console
                        Panel = rich_panel.Panel
                        Console().print(Panel.fit(f"projects_scanned={len(pids)}", title="Claims Cleanup", border_style="cyan"))
                    except Exception:
                        pass
                    with contextlib.suppress(Exception):
                        structlog.get_logger("tasks").info("claims_cleanup", projects_scanned=len(pids))
                except Exception:
                    pass
                await asyncio.sleep(settings.claims_cleanup_interval_seconds)

        async def _worker_ack_ttl() -> None:
            import datetime as _dt
            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        result = await session.execute(text(
                            """
                            SELECT m.id, m.project_id, m.created_ts, mr.agent_id
                            FROM messages m
                            JOIN message_recipients mr ON mr.message_id = m.id
                            WHERE m.ack_required = 1 AND mr.ack_ts IS NULL
                            """
                        ))
                        rows = result.fetchall()
                    now = _dt.datetime.now(_dt.timezone.utc)
                    for mid, project_id, created_ts, agent_id in rows:
                        age = (now - created_ts).total_seconds()
                        if age >= settings.ack_ttl_seconds:
                            try:
                                rich_console = importlib.import_module("rich.console")
                                rich_panel = importlib.import_module("rich.panel")
                                rich_text = importlib.import_module("rich.text")
                                Console = rich_console.Console
                                Panel = rich_panel.Panel
                                Text = rich_text.Text
                                con = Console()
                                body = Text.assemble(
                                    ("message_id: ", "cyan"), (str(mid), "white"), "\n",
                                    ("agent_id: ", "cyan"), (str(agent_id), "white"), "\n",
                                    ("project_id: ", "cyan"), (str(project_id), "white"), "\n",
                                    ("age_s: ", "cyan"), (str(int(age)), "white"), "\n",
                                    ("ttl_s: ", "cyan"), (str(settings.ack_ttl_seconds), "white"),
                                )
                                con.print(Panel(body, title="ACK Overdue", border_style="red"))
                            except Exception:
                                print(f"ack-warning message_id={mid} project_id={project_id} agent_id={agent_id} age_s={int(age)} ttl_s={settings.ack_ttl_seconds}")
                            with contextlib.suppress(Exception):
                                structlog.get_logger("tasks").warning(
                                    "ack_overdue",
                                    message_id=str(mid),
                                    project_id=str(project_id),
                                    agent_id=str(agent_id),
                                    age_s=int(age),
                                    ttl_s=int(settings.ack_ttl_seconds),
                                )
                            if settings.ack_escalation_enabled:
                                mode = (settings.ack_escalation_mode or "log").lower()
                                if mode == "claim":
                                    try:
                                        y_dir = created_ts.strftime("%Y")
                                        m_dir = created_ts.strftime("%m")
                                        # Resolve recipient name
                                        async with get_session() as s_lookup:
                                            name_row = await s_lookup.execute(text("SELECT name FROM agents WHERE id = :aid"), {"aid": agent_id})
                                            name_res = name_row.fetchone()
                                        recipient_name = name_res[0] if name_res and name_res[0] else "*"
                                        pattern = f"agents/{recipient_name}/inbox/{y_dir}/{m_dir}/*.md" if recipient_name != "*" else f"agents/*/inbox/{y_dir}/{m_dir}/*.md"
                                        holder_agent_id = int(agent_id)
                                        if settings.ack_escalation_claim_holder_name:
                                            async with get_session() as s_holder:
                                                hid_row = await s_holder.execute(
                                                    text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                                                    {"pid": project_id, "name": settings.ack_escalation_claim_holder_name},
                                                )
                                                hid = hid_row.scalar_one_or_none()
                                                if isinstance(hid, int):
                                                    holder_agent_id = hid
                                                else:
                                                    # Auto-create ops holder in DB and write profile.json
                                                    await s_holder.execute(text(
                                                        "INSERT INTO agents(project_id, name, program, model, task_description, inception_ts, last_active_ts) VALUES (:pid, :name, :program, :model, :task, :ts, :ts)"
                                                    ), {
                                                        "pid": project_id,
                                                        "name": settings.ack_escalation_claim_holder_name,
                                                        "program": "ops",
                                                        "model": "system",
                                                        "task": "ops-escalation",
                                                        "ts": now,
                                                    })
                                                    await s_holder.commit()
                                                    hid_row2 = await s_holder.execute(
                                                        text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                                                        {"pid": project_id, "name": settings.ack_escalation_claim_holder_name},
                                                    )
                                                    hid2 = hid_row2.scalar_one_or_none()
                                                    if isinstance(hid2, int):
                                                        holder_agent_id = hid2
                                                        # Write profile.json to archive
                                                        archive = await ensure_archive(settings, (await _project_slug_from_id(project_id)) or "")
                                                        async with AsyncFileLock(archive.lock_path):
                                                            await write_agent_profile(archive, {
                                                                "id": holder_agent_id,
                                                                "name": settings.ack_escalation_claim_holder_name,
                                                                "program": "ops",
                                                                "model": "system",
                                                                "project_slug": (await _project_slug_from_id(project_id)) or "",
                                                                "inception_ts": now.astimezone().isoformat(),
                                                                "inception_iso": now.astimezone().isoformat(),
                                                                "task": "ops-escalation",
                                                            })
                                        async with get_session() as s2:
                                            await s2.execute(text(
                                                """
                                                INSERT INTO claims(project_id, agent_id, path_pattern, exclusive, reason, created_ts, expires_ts)
                                                VALUES (:pid, :holder, :pattern, :exclusive, :reason, :cts, :ets)
                                                """
                                            ), {
                                                "pid": project_id,
                                                "holder": holder_agent_id,
                                                "pattern": pattern,
                                                "exclusive": 1 if settings.ack_escalation_claim_exclusive else 0,
                                                "reason": "ack-overdue",
                                                "cts": now,
                                                "ets": now + _dt.timedelta(seconds=settings.ack_escalation_claim_ttl_seconds),
                                            })
                                            await s2.commit()
                                        # Also write JSON artifact to archive
                                        project_slug = (await _project_slug_from_id(project_id)) or ""
                                        archive = await ensure_archive(settings, project_slug)
                                        expires_at = now + _dt.timedelta(seconds=settings.ack_escalation_claim_ttl_seconds)
                                        async with AsyncFileLock(archive.lock_path):
                                            await write_claim_record(archive, {
                                                "project": project_slug,
                                                "agent": settings.ack_escalation_claim_holder_name or "ops",
                                                "path_pattern": pattern,
                                                "exclusive": settings.ack_escalation_claim_exclusive,
                                                "reason": "ack-overdue",
                                                "created_ts": now.astimezone().isoformat(),
                                                "expires_ts": expires_at.astimezone().isoformat(),
                                            })
                                    except Exception:
                                        pass
                except Exception:
                    pass
                await asyncio.sleep(settings.ack_ttl_scan_interval_seconds)

        async def _worker_tool_metrics() -> None:
            log = structlog.get_logger("tool.metrics")
            while True:
                try:
                    snapshot = _tool_metrics_snapshot()
                    if snapshot:
                        log.info("tool_metrics_snapshot", tools=snapshot)
                except Exception:
                    pass
                await asyncio.sleep(max(5, settings.tool_metrics_emit_interval_seconds))

        async def _worker_retention_quota() -> None:
            import datetime as _dt
            from pathlib import Path as _Path
            while True:
                from contextlib import suppress as _suppress
                with _suppress(Exception):
                    storage_root = _Path(settings.storage.root).expanduser().resolve()
                    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(settings.retention_max_age_days))
                    old_messages = 0
                    total_attach_bytes = 0
                    per_project_attach: dict[str, int] = {}
                    per_project_inbox_counts: dict[str, int] = {}
                    # Compile ignore patterns once per loop
                    import fnmatch as _fnmatch
                    ignore_patterns = list(getattr(settings, "retention_ignore_project_patterns", []) or [])
                    for proj_dir in storage_root.iterdir() if storage_root.exists() else []:
                        if not proj_dir.is_dir():
                            continue
                        proj_name = proj_dir.name
                        # Skip test/demo projects in real server runs
                        if any(_fnmatch.fnmatch(proj_name, pat) for pat in ignore_patterns):
                            continue
                        msg_root = proj_dir / "messages"
                        if msg_root.exists():
                            for ydir in msg_root.iterdir():
                                for mdir in (ydir.iterdir() if ydir.is_dir() else []):
                                    for f in (mdir.iterdir() if mdir.is_dir() else []):
                                        if f.suffix.lower() == ".md":
                                            with _suppress(Exception):
                                                ts = _dt.datetime.fromtimestamp(f.stat().st_mtime, _dt.timezone.utc)
                                                if ts < cutoff:
                                                    old_messages += 1
                        # Count per-agent inbox files (agents/*/inbox/YYYY/MM/*.md)
                        inbox_root = proj_dir / "agents"
                        if inbox_root.exists():
                            count_inbox = 0
                            for f in inbox_root.rglob("inbox/*/*/*.md"):
                                with _suppress(Exception):
                                    if f.is_file():
                                        count_inbox += 1
                            per_project_inbox_counts[proj_name] = count_inbox
                        att_root = proj_dir / "attachments"
                        if att_root.exists():
                            for sub in att_root.rglob("*.webp"):
                                with _suppress(Exception):
                                    sz = sub.stat().st_size
                                    total_attach_bytes += sz
                                    per_project_attach[proj_name] = per_project_attach.get(proj_name, 0) + sz
                    structlog.get_logger("maintenance").info(
                        "retention_quota_report",
                        old_messages=old_messages,
                        retention_max_age_days=int(settings.retention_max_age_days),
                        total_attachments_bytes=total_attach_bytes,
                        quota_limit_bytes=int(settings.quota_attachments_limit_bytes),
                        per_project_attach=per_project_attach,
                        per_project_inbox_counts=per_project_inbox_counts,
                    )
                    # Quota alerts
                    limit_b = int(settings.quota_attachments_limit_bytes)
                    inbox_limit = int(settings.quota_inbox_limit_count)
                    if limit_b > 0:
                        for proj, used in per_project_attach.items():
                            if used >= limit_b:
                                structlog.get_logger("maintenance").warning(
                                    "quota_attachments_exceeded", project=proj, used_bytes=used, limit_bytes=limit_b
                                )
                    if inbox_limit > 0:
                        for proj, cnt in per_project_inbox_counts.items():
                            if cnt >= inbox_limit:
                                structlog.get_logger("maintenance").warning(
                                    "quota_inbox_exceeded", project=proj, inbox_count=cnt, limit=inbox_limit
                                )
                await asyncio.sleep(max(60, settings.retention_report_interval_seconds))

        tasks = []
        if settings.claims_cleanup_enabled:
            tasks.append(asyncio.create_task(_worker_cleanup()))
        if settings.ack_ttl_enabled:
            tasks.append(asyncio.create_task(_worker_ack_ttl()))
        if settings.tool_metrics_emit_enabled:
            tasks.append(asyncio.create_task(_worker_tool_metrics()))
        if settings.retention_report_enabled or settings.quota_enabled:
            tasks.append(asyncio.create_task(_worker_retention_quota()))
        fastapi_app.state._background_tasks = tasks

    async def _shutdown() -> None:  # pragma: no cover - service lifecycle
        tasks = getattr(fastapi_app.state, "_background_tasks", [])
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(Exception):
                await task

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan_context(app: FastAPI):
        # Ensure the mounted MCP app initializes its internal task group
        async with mcp_http_app.lifespan(mcp_http_app):
            await _startup()
            try:
                yield
            finally:
                await _shutdown()

    # Now construct FastAPI with the composed lifespan so ASGI transports run it
    fastapi_app = FastAPI(lifespan=lifespan_context)

    # Simple request logging (configurable)
    if settings.http.request_log_enabled:
        import time as _time
        class RequestLoggingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
                start = _time.time()
                response = await call_next(request)
                dur_ms = int((_time.time() - start) * 1000)
                method = request.method
                path = request.url.path
                status_code = getattr(response, "status_code", 0)
                client = request.client.host if request.client else "-"
                with contextlib.suppress(Exception):
                    structlog.get_logger("http").info(
                        "request",
                        method=method,
                        path=path,
                        status=status_code,
                        duration_ms=dur_ms,
                        client_ip=client,
                    )
                try:
                    rich_console = importlib.import_module("rich.console")
                    rich_panel = importlib.import_module("rich.panel")
                    rich_text = importlib.import_module("rich.text")
                    Console = rich_console.Console
                    Panel = rich_panel.Panel
                    Text = rich_text.Text
                    console = Console(width=100)
                    title = Text.assemble(
                        (method, "bold blue"),
                        ("  "),
                        (path, "bold white"),
                        ("  "),
                        (f"{status_code}", "bold green" if 200 <= status_code < 400 else "bold red"),
                        ("  "),
                        (f"{dur_ms}ms", "bold yellow"),
                    )
                    body = Text.assemble(
                        ("client: ", "cyan"), (client, "white"),
                    )
                    console.print(Panel(body, title=title, border_style="dim"))
                except Exception:
                    print(f"http method={method} path={path} status={status_code} ms={dur_ms} client={client}")
                return response
        fastapi_app.add_middleware(RequestLoggingMiddleware)

    # Unified JWT/RBAC and robust rate limiter middleware
    if settings.http.rate_limit_enabled or getattr(settings.http, "jwt_enabled", False) or getattr(settings.http, "rbac_enabled", True):
        fastapi_app.add_middleware(SecurityAndRateLimitMiddleware, settings=settings)
    if settings.http.bearer_token:
        fastapi_app.add_middleware(BearerAuthMiddleware, token=settings.http.bearer_token)

    # Optional CORS
    if settings.cors.enabled:
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.origins or ["*"],
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods or ["*"],
            allow_headers=settings.cors.allow_headers or ["*"],
        )

    # Health endpoints
    @fastapi_app.get("/health/liveness")
    async def liveness() -> JSONResponse:
        return JSONResponse({"status": "alive"})
    @fastapi_app.get("/health/readiness")
    async def readiness() -> JSONResponse:
        try:
            await readiness_check()
        except Exception as exc:
            try:
                rich_console = importlib.import_module("rich.console")
                rich_panel = importlib.import_module("rich.panel")
                Console = rich_console.Console
                Panel = rich_panel.Panel
                Console().print(Panel.fit(str(exc), title="Readiness Error", border_style="red"))
            except Exception:
                pass
            with contextlib.suppress(Exception):
                structlog.get_logger("health").error("readiness_error", error=str(exc))
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return JSONResponse({"status": "ready"})

    # A minimal stateless ASGI adapter that does not rely on ASGI lifespan management
    # and runs a fresh StreamableHTTP transport per request.
    from mcp.server.streamable_http import StreamableHTTPServerTransport

    class StatelessMCPASGIApp:
        def __init__(self, mcp_server) -> None:
            self._server = mcp_server

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("type") != "http":
                res = JSONResponse({"detail": "Not Found"}, status_code=404)
                await res(scope, receive, send)
                return

            # Ensure Accept and Content-Type headers are present per StreamableHTTP expectations
            headers = list(scope.get("headers") or [])
            def _has_header(key: bytes) -> bool:
                lk = key.lower()
                return any(h[0].lower() == lk for h in headers)

            # Ensure both JSON and SSE are present; httpx defaults no Accept header
            headers = [(k, v) for (k, v) in headers if k.lower() != b"accept"]
            headers.append((b"accept", b"application/json, text/event-stream"))
            if scope.get("method") == "POST" and not _has_header(b"content-type"):
                headers.append((b"content-type", b"application/json"))
            new_scope = dict(scope)
            new_scope["headers"] = headers

            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=None,
                is_json_response_enabled=True,
                event_store=None,
                security_settings=None,
            )

            async with http_transport.connect() as streams:
                read_stream, write_stream = streams
                server_task = asyncio.create_task(
                    self._server._mcp_server.run(
                        read_stream,
                        write_stream,
                        self._server._mcp_server.create_initialization_options(),
                        stateless=True,
                    )
                )
                # Wrap ASGI send to capture and normalize JSON-RPC to simple tool result
                async def send_wrapper(message: dict) -> None:
                    if message.get("type") == "http.response.body" and message.get("more_body") in (False, None):
                        body_bytes = message.get("body") or b""
                        try:
                            payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
                        except Exception:
                            payload = None
                        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                            result_obj = payload["result"]
                            # If MCP content blocks present, try to unwrap first text block assuming JSON
                            content = result_obj.get("content")
                            if isinstance(content, list) and content:
                                first = content[0]
                                text_val = first.get("text") if isinstance(first, dict) else None
                                if isinstance(text_val, str):
                                    with contextlib.suppress(Exception):
                                        unwrapped = json.loads(text_val)
                                        payload["result"] = unwrapped
                                        body_bytes = json.dumps(payload).encode("utf-8")
                                        message = {**message, "body": body_bytes}
                    await send(message)

                try:
                    await http_transport.handle_request(new_scope, receive, send_wrapper)
                finally:
                    with contextlib.suppress(Exception):
                        await http_transport.terminate()
                    with contextlib.suppress(Exception):
                        await server_task

    # Mount at both '/base' and '/base/' to tolerate either form from clients/tests
    mount_base = settings.http.path or "/mcp"
    if not mount_base.startswith("/"):
        mount_base = "/" + mount_base
    base_no_slash = mount_base.rstrip("/") or "/"
    base_with_slash = base_no_slash if base_no_slash == "/" else base_no_slash + "/"
    stateless_app = StatelessMCPASGIApp(server)
    with contextlib.suppress(Exception):
        fastapi_app.mount(base_no_slash, stateless_app)
    with contextlib.suppress(Exception):
        fastapi_app.mount(base_with_slash, stateless_app)

    # Expose composed lifespan via router
    fastapi_app.router.lifespan_context = lifespan_context

    # Add a direct route at the base path without redirect to tolerate clients omitting trailing slash
    @fastapi_app.post(base_no_slash)
    async def _base_passthrough(request: Request) -> JSONResponse:
        # Re-dispatch to mounted stateless app by calling it directly
        response_body = {}
        status_code = 200
        headers: dict[str, str] = {}
        async def _send(message: dict) -> None:
            nonlocal response_body, status_code, headers
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 200))
                hdrs = message.get("headers") or []
                for k, v in hdrs:
                    headers[k.decode("latin1")] = v.decode("latin1")
            elif message.get("type") == "http.response.body":
                body = message.get("body") or b""
                try:
                    response_body = json.loads(body.decode("utf-8")) if body else {}
                except Exception:
                    response_body = {}
        await stateless_app(
            {**request.scope, "path": base_with_slash},  # ensure mounted path
            request.receive,
            _send,
        )
        return JSONResponse(response_body, status_code=status_code, headers=headers)
    return fastapi_app


def main() -> None:
    """Run the HTTP transport using settings-specified host/port."""

    parser = argparse.ArgumentParser(description="Run the MCP Agent Mail HTTP transport")
    parser.add_argument("--host", help="Override HTTP host", default=None)
    parser.add_argument("--port", help="Override HTTP port", type=int, default=None)
    parser.add_argument("--log-level", help="Uvicorn log level", default="info")
    # Be tolerant of extraneous argv when invoked under test runners
    args, _unknown = parser.parse_known_args()

    settings = get_settings()
    host = args.host or settings.http.host
    port = args.port or settings.http.port

    app = build_http_app(settings)
    # Disable WebSockets when running the service directly; HTTP-only transport
    import inspect as _inspect
    _sig = _inspect.signature(uvicorn.run)
    _kwargs: dict[str, Any] = {"host": host, "port": port, "log_level": args.log_level}
    if "ws" in _sig.parameters:
        _kwargs["ws"] = "none"
    uvicorn.run(app, **_kwargs)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
