from __future__ import annotations

from typing import Any

import pytest

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


@pytest.fixture
def bench_factory(isolated_env):
    async def _factory(label: str, seed: int) -> BenchHarness:
        await ensure_schema()
        mcp = build_mcp_server()

        async def call_tool(tool_name: str, args: dict[str, Any]) -> Any:
            _contents, structured = await mcp._mcp_call_tool(tool_name, args)
            return structured

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
        return BenchHarness(mcp=mcp, project_key=project_key, agent_name=agent_name, call_tool=call_tool)

    return _factory
