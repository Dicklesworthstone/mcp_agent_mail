"""Minimal client bootstrap that honours the tooling directory guidance.

This script is intentionally verbose so MCP client implementers can copy/paste
the parts they need. It demonstrates:

1. Fetching `resource://tooling/directory` and selecting an active cluster.
2. Optionally polling `resource://tooling/metrics` for dashboards.
3. Falling back to workflow macros when the selected model is "small".

The code uses the same `fastmcp.Client` class that the test-suite relies on,
so it can run directly against a locally launched `serve-http` instance.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastmcp import Client


def _select_cluster(directory_payload: dict[str, Any], cluster_name: str) -> dict[str, Any]:
    for cluster in directory_payload.get("clusters", []):
        if cluster.get("name") == cluster_name:
            return cluster
    raise RuntimeError(f"Cluster '{cluster_name}' not found in directory payload")


async def main() -> None:
    # Supply capability tokens for this agent (examples/capability template in
    # deploy/capabilities/agent_capabilities.example.yaml). Most MCP client
    # libraries accept metadata at connection time; refer to your client docs.
    async with Client("http://127.0.0.1:8765/mcp/") as client:
        directory_blocks = await client.read_resource("resource://tooling/directory")
        directory_payload = json.loads(getattr(directory_blocks[0], "text", "{}"))
        print("==> Loaded tooling directory; clusters available:")
        for cluster in directory_payload.get("clusters", []):
            print(f" - {cluster['name']} ({len(cluster['tools'])} tools)")

        # Pick a workflow for this session
        active_cluster = _select_cluster(directory_payload, "Messaging Lifecycle")

        # Determine which tools to activate. The simple heuristic below hides
        # high-complexity tools unless the underlying model is considered
        # "large". Replace this with your own routing logic.
        model_size = "small"
        enabled_tools: list[str] = []
        for tool in active_cluster["tools"]:
            complexity = tool.get("complexity", "medium")
            if model_size == "small" and complexity == "high":
                continue
            enabled_tools.append(tool["name"])

        # Always bolt on the workflow macros when using smaller models.
        if model_size == "small":
            for macro_name in ("macro_start_session", "macro_prepare_thread"):
                if macro_name not in enabled_tools:
                    enabled_tools.append(macro_name)

        print("==> Enable the following tools in the agent runtime:")
        for name in enabled_tools:
            print(f"   - {name}")

        # Optionally read metrics for dashboards
        metrics_blocks = await client.read_resource("resource://tooling/metrics")
        print("==> Current tool metrics snapshot:")
        print(getattr(metrics_blocks[0], "text", "{}"))

        # Finally run whatever workflow you need. As an example, call the macro
        # to bootstrap a session.
        response = await client.call_tool(
            "macro_start_session",
            {
                "human_key": "/abs/path/backend",
                "program": "codex",
                "model": "gpt-5-small",
                "reserve_paths": ["src/app.py"],
            },
        )
        print("==> macro_start_session result:")
        print(response.content)


if __name__ == "__main__":
    asyncio.run(main())
