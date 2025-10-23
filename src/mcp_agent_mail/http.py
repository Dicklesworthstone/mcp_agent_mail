"""HTTP transport helpers wrapping FastMCP with FastAPI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .app import _expire_stale_claims, build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .storage import AsyncFileLock, ensure_archive, write_claim_record


async def _project_slug_from_id(pid: int | None) -> str | None:
    if pid is None:
        return None
    async with get_session() as session:
        row = await session.execute(text("SELECT slug FROM projects WHERE id = :pid"), {"pid": pid})
        res = row.fetchone()
        return res[0] if res and res[0] else None

__all__ = ["build_http_app", "main"]


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


async def readiness_check() -> None:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(text("SELECT 1"))


def build_http_app(settings: Settings, server=None) -> FastAPI:
    fastapi_app = FastAPI()
    if server is None:
        server = build_mcp_server()

    # Simple request logging (configurable)
    if settings.http.request_log_enabled:
        import time

        class RequestLoggingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
                start = time.time()
                response = await call_next(request)
                dur_ms = int((time.time() - start) * 1000)
                method = request.method
                path = request.url.path
                status_code = getattr(response, "status_code", 0)
                client = request.client.host if request.client else "-"
                try:
                    from rich.console import Console  # type: ignore
                    from rich.panel import Panel  # type: ignore
                    from rich.text import Text  # type: ignore

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

    # Lightweight IP-based rate limiting (best-effort, per-process)
    if settings.http.rate_limit_enabled and settings.http.rate_limit_per_minute > 0:
        from collections import defaultdict, deque
        from time import time

        window_seconds = 60
        limit = int(settings.http.rate_limit_per_minute)
        hits: dict[str, deque[float]] = defaultdict(deque)

        class RateLimitMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.method == "OPTIONS":
                    return await call_next(request)
                if request.url.path.startswith("/health/"):
                    return await call_next(request)
                client_ip = request.client.host if request.client else "unknown"
                now = time()
                dq = hits[client_ip]
                while dq and now - dq[0] >= window_seconds:
                    dq.popleft()
                if len(dq) >= limit:
                    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
                dq.append(now)
                return await call_next(request)

        fastapi_app.add_middleware(RateLimitMiddleware)

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
            from opentelemetry import trace  # type: ignore
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )
            from opentelemetry.instrumentation.fastapi import (  # type: ignore
                FastAPIInstrumentor,
            )
            from opentelemetry.sdk.resources import Resource  # type: ignore
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore
            from opentelemetry.sdk.trace.export import (  # type: ignore
                BatchSpanProcessor,
            )

            resource = Resource.create({"service.name": settings.http.otel_service_name})
            provider = TracerProvider(resource=resource)
            span_exporter = OTLPSpanExporter(endpoint=settings.http.otel_exporter_otlp_endpoint or None)
            processor = BatchSpanProcessor(span_exporter)
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)
            FastAPIInstrumentor.instrument_app(fastapi_app)
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
                                import structlog  # type: ignore
                                structlog.get_logger().warning(
                                    "ack_overdue",
                                    message_id=mid,
                                    agent_id=agent_id,
                                    project_id=project_id,
                                    age_seconds=int(age),
                                    ttl_seconds=settings.ack_ttl_seconds,
                                )
                            except Exception:
                                print(f"ack-warning message_id={mid} project_id={project_id} agent_id={agent_id} age_s={int(age)} ttl_s={settings.ack_ttl_seconds}")
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
                                        archive = await ensure_archive(settings, (await _project_slug_from_id(project_id)) or "")
                                        async with AsyncFileLock(archive.lock_path):
                                            await write_claim_record(archive, {
                                                "agent": settings.ack_escalation_claim_holder_name or recipient_name,
                                                "path": pattern,
                                                "exclusive": bool(settings.ack_escalation_claim_exclusive),
                                                "reason": "ack-overdue",
                                                "created": now.astimezone().isoformat(),
                                                "expires": (now + _dt.timedelta(seconds=settings.ack_escalation_claim_ttl_seconds)).astimezone().isoformat(),
                                            })
                                    except Exception:
                                        pass
                except Exception:
                    pass
                await asyncio.sleep(settings.ack_ttl_scan_interval_seconds)

        tasks = []
        if settings.claims_cleanup_enabled:
            tasks.append(asyncio.create_task(_worker_cleanup()))
        if settings.ack_ttl_enabled:
            tasks.append(asyncio.create_task(_worker_ack_ttl()))
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
