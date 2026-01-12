from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema

from .utils import BenchHarness, benchmark_enabled_reason


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") and "benchmark" in config.getoption("-m"):
        return
    if benchmark_enabled_reason().startswith("Benchmarks enabled"):
        return
    skip_marker = pytest.mark.skip(reason=benchmark_enabled_reason())
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip_marker)


@asynccontextmanager
async def _bench_context(label: str, seed: int):
    """Context manager for benchmark setup with proper FastMCP Client."""
    await ensure_schema()
    mcp = build_mcp_server()

    async with Client(mcp) as client:

        async def call_tool(tool_name: str, args: dict[str, Any]) -> Any:
            result = await client.call_tool(tool_name, args)
            return result.data if hasattr(result, "data") else result

        async def read_resource(uri: str) -> Any:
            result = await client.read_resource(uri)
            return result

        project_key = f"/bench-{label}-{seed}"
        await call_tool("ensure_project", {"human_key": project_key})
        agent_result = await call_tool(
            "create_agent_identity",
            {
                "project_key": project_key,
                "program": "benchmark",
                "model": "test",
                "task_description": f"Benchmark agent for {label}",
            },
        )
        agent_name = agent_result["name"]
        yield BenchHarness(
            mcp=mcp,
            project_key=project_key,
            agent_name=agent_name,
            call_tool=call_tool,
            read_resource=read_resource,
        )


@pytest.fixture
def bench_factory(isolated_env):
    return _bench_context
