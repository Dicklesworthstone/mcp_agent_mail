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
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
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
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
            token = auth_header.split(" ", 1)[1].strip()
            claims_dict = await self._decode_jwt(token)
            if claims_dict is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
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
                        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
                else:
                    if tool_name in self._readonly_tools:
                        if not is_reader and not is_writer:
                            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
                    else:
                        if not is_writer:
                            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

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
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

        return await call_next(request)


async def readiness_check() -> None:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(text("SELECT 1"))


def build_http_app(settings: Settings, server=None) -> FastAPI:
    # Configure logging once
    _configure_logging(settings)
    fastapi_app = FastAPI()
    if server is None:
        server = build_mcp_server()
    # Rich traceback (optional)
    if getattr(settings, "log_rich_enabled", False) and getattr(settings, "log_include_trace", False):
        try:
            rich_tb_mod = importlib.import_module("rich.traceback")
            rich_traceback_install = rich_tb_mod.install
            rich_traceback_install(show_locals=False)
        except Exception:
            pass

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

    # Optional CORS (add last so it can handle preflight and attach headers to errors)
    if settings.cors.enabled:
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.origins or ["*"],
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods or ["*"],
            allow_headers=settings.cors.allow_headers or ["*"],
        )

    # Optional OpenTelemetry auto-instrumentation for FastAPI
    if settings.http.otel_enabled:
        try:
            ot_trace = importlib.import_module("opentelemetry.trace")
            ot_export_http = importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
            ot_fastapi = importlib.import_module("opentelemetry.instrumentation.fastapi")
            ot_resources = importlib.import_module("opentelemetry.sdk.resources")
            ot_trace_sdk = importlib.import_module("opentelemetry.sdk.trace")
            ot_trace_export = importlib.import_module("opentelemetry.sdk.trace.export")

            Resource = getattr(ot_resources, "Resource", None)
            TracerProvider = getattr(ot_trace_sdk, "TracerProvider", None)
            OTLPSpanExporter = getattr(ot_export_http, "OTLPSpanExporter", None)
            BatchSpanProcessor = getattr(ot_trace_export, "BatchSpanProcessor", None)
            FastAPIInstrumentor = getattr(ot_fastapi, "FastAPIInstrumentor", None)
            set_tracer_provider = getattr(ot_trace, "set_tracer_provider", None)

            if not all([Resource, TracerProvider, OTLPSpanExporter, BatchSpanProcessor, FastAPIInstrumentor, set_tracer_provider]):
                raise RuntimeError("opentelemetry modules unavailable")

            from typing import Any, cast
            ResourceT = cast(Any, Resource)
            TracerProviderT = cast(Any, TracerProvider)
            OTLPSpanExporterT = cast(Any, OTLPSpanExporter)
            BatchSpanProcessorT = cast(Any, BatchSpanProcessor)
            FastAPIInstrumentorT = cast(Any, FastAPIInstrumentor)
            set_tracer_provider_fn = cast(Any, set_tracer_provider)

            resource = ResourceT.create({"service.name": settings.http.otel_service_name})
            provider = TracerProviderT(resource=resource)
            span_exporter = OTLPSpanExporterT(endpoint=settings.http.otel_exporter_otlp_endpoint or None)
            processor = BatchSpanProcessorT(span_exporter)
            provider.add_span_processor(processor)
            set_tracer_provider_fn(provider)
            FastAPIInstrumentorT.instrument_app(fastapi_app)
        except Exception:  # pragma: no cover - optional dependency path
            pass

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

    mcp_http_app = server.http_app(path=settings.http.path)
    fastapi_app.mount(settings.http.path.rstrip("/"), mcp_http_app)

    # Optional periodic claims cleanup background task and ACK TTL warnings using lifespan
    async def _startup() -> None:  # pragma: no cover - service lifecycle
        if not (settings.claims_cleanup_enabled or settings.ack_ttl_enabled):
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
                    # Log a compact Rich panel with scan summary
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
                                                "agent": settings.ack_escalation_claim_holder_name or recipient_name,
                                                "project": project_slug,
                                                "path_pattern": pattern,
                                                "exclusive": bool(settings.ack_escalation_claim_exclusive),
                                                "reason": "ack-overdue",
                                                "created_ts": now.isoformat(),
                                                "expires_ts": expires_at.isoformat(),
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

        tasks = []
        if settings.claims_cleanup_enabled:
            tasks.append(asyncio.create_task(_worker_cleanup()))
        if settings.ack_ttl_enabled:
            tasks.append(asyncio.create_task(_worker_ack_ttl()))
        if settings.tool_metrics_emit_enabled:
            tasks.append(asyncio.create_task(_worker_tool_metrics()))
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
        await _startup()
        try:
            yield
        finally:
            await _shutdown()

    fastapi_app.router.lifespan_context = lifespan_context
    return fastapi_app


def main() -> None:
    """Run the HTTP transport using settings-specified host/port."""

    parser = argparse.ArgumentParser(description="Run the MCP Agent Mail HTTP transport")
    parser.add_argument("--host", help="Override HTTP host", default=None)
    parser.add_argument("--port", help="Override HTTP port", type=int, default=None)
    parser.add_argument("--log-level", help="Uvicorn log level", default="info")
    args = parser.parse_args()

    settings = get_settings()
    host = args.host or settings.http.host
    port = args.port or settings.http.port

    app = build_http_app(settings)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
