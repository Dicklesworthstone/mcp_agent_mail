"""Top-level package for the MCP Agent Mail server."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from typing import Any, cast

# Python 3.14 warns when third-party code calls asyncio.iscoroutinefunction.
# Patch it globally to the inspect implementation before importing submodules.
asyncio.iscoroutinefunction = inspect.iscoroutinefunction  # type: ignore[attr-defined]

_app_module = cast(Any, importlib.import_module(".app", __name__))
build_mcp_server = _app_module.build_mcp_server

__all__ = ["build_mcp_server"]
