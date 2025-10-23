"""HTTP transport helpers wrapping FastMCP with FastAPI."""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from sqlalchemy import text

from .app import build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .app import _expire_stale_claims

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
                # Minimal, structured-like log line
                method = request.method
                path = request.url.path
                status_code = getattr(response, "status_code", 0)
                client = request.client.host if request.client else "-"
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
            from opentelemetry.instrumentation.fastapi import (  # type: ignore
                FastAPIInstrumentor,
            )
            from opentelemetry.sdk.resources import Resource  # type: ignore
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore
            from opentelemetry.sdk.trace.export import (  # type: ignore
                BatchSpanProcessor,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
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

    # Optional periodic claims cleanup background task
    if settings.claims_cleanup_enabled:
        import asyncio

        @fastapi_app.on_event("startup")
        async def _start_cleanup_task() -> None:  # pragma: no cover - service lifecycle
            async def _worker() -> None:
                while True:
                    try:
                        await ensure_schema()
                        async with get_session() as session:
                            rows = await session.execute(text("SELECT DISTINCT project_id FROM claims"))
                            pids = [r[0] for r in rows.fetchall() if r[0] is not None]
                        for pid in pids:
                            try:
                                await _expire_stale_claims(pid)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    await asyncio.sleep(settings.claims_cleanup_interval_seconds)

            fastapi_app.state._claims_cleanup_task = asyncio.create_task(_worker())

        @fastapi_app.on_event("shutdown")
        async def _stop_cleanup_task() -> None:  # pragma: no cover - service lifecycle
            task = getattr(fastapi_app.state, "_claims_cleanup_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
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
