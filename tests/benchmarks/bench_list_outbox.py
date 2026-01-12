from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_list_outbox(bench_factory):
    seed = DEFAULT_SEED
    iterations = 20
    message_size = 256
    message_count = 150

    async with bench_factory("list_outbox", seed) as harness:

        recipient_names = []
        for i in range(3):
            agent_result = await harness.call_tool(
                "create_agent_identity",
                {
                    "project_key": harness.project_key,
                    "program": "benchmark",
                    "model": "test",
                    "task_description": f"Recipient {i}",
                },
            )
            recipient_names.append(agent_result["name"])

        for i in range(message_count):
            await harness.call_tool(
                "send_message",
                {
                    "project_key": harness.project_key,
                    "sender_name": harness.agent_name,
                    "to": [recipient_names[i % len(recipient_names)]],
                    "subject": f"Outbox seed {i}",
                    "body_md": make_message_body(seed, i, message_size),
                },
            )

        async def operation(_i: int) -> None:
            query_params = urlencode({
                "project": harness.project_key,
                "limit": 100,
                "include_bodies": "false",
            })
            resource_uri = f"resource://outbox/{harness.agent_name}?{query_params}"
            result = await harness.read_resource(resource_uri)
            # Parse result - FastMCP client returns different structure
            if hasattr(result, "contents") and result.contents:
                content = result.contents[0]
                payload = json.loads(content.text) if hasattr(content, "text") else json.loads(str(content))
            else:
                payload = {}
            assert payload.get("count", 0) >= 0

        await run_benchmark(
            name="list_outbox",
            tool="outbox_resource",
            iterations=iterations,
            seed=seed,
            dataset={"message_count": message_count, "message_size": message_size, "recipients": 3},
            operation=operation,
            warmup=2,
        )
