from __future__ import annotations

import json

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_list_outbox(bench_factory):
    seed = DEFAULT_SEED
    iterations = 20
    message_size = 256
    message_count = 150

    harness = await bench_factory("list_outbox", seed)

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
        resource_uri = (
            f"resource://outbox/{harness.agent_name}"
            f"?project={harness.project_key}&limit=100&include_bodies=false"
        )
        contents = await harness.mcp._mcp_read_resource(resource_uri)
        payload = json.loads(contents[0].content)
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
