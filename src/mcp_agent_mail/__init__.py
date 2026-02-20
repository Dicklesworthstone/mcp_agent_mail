"""Top-level package for the MCP Agent Mail server."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast

# Python 3.14 warns when third-party code calls asyncio.iscoroutinefunction.
# Patch it globally to the inspect implementation before importing submodules.
asyncio.iscoroutinefunction = cast(Any, inspect.iscoroutinefunction)

def build_mcp_server() -> Any:
    """Lazily import and build the FastMCP server to avoid heavy module import costs."""
    from .app import build_mcp_server as _build_mcp_server
    return _build_mcp_server()

__all__ = ["build_mcp_server"]
