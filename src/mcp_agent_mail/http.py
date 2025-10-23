"""HTTP transport helpers wrapping FastMCP with FastAPI."""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from .app import build_mcp_server
from .config import Settings, get_settings
from .db import ensure_schema, get_session

__all__ = ["build_http_app", "main"]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
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

    if settings.http.bearer_token:
        fastapi_app.add_middleware(BearerAuthMiddleware, token=settings.http.bearer_token)

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
