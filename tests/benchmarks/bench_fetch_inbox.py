from __future__ import annotations

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_fetch_inbox(bench_factory):
    seed = DEFAULT_SEED
    iterations = 25
    message_size = 256
    message_count = 200

    async with bench_factory("fetch_inbox", seed) as harness:

        for i in range(message_count):
            await harness.call_tool(
                "send_message",
                {
                    "project_key": harness.project_key,
                    "sender_name": harness.agent_name,
                    "to": [harness.agent_name],
                    "subject": f"Inbox seed {i}",
                    "body_md": make_message_body(seed, i, message_size),
                },
            )

        async def operation(_i: int) -> None:
            await harness.call_tool(
                "fetch_inbox",
                {
                    "project_key": harness.project_key,
                    "agent_name": harness.agent_name,
                    "limit": 100,
                },
            )

        await run_benchmark(
            name="fetch_inbox",
            tool="fetch_inbox",
            iterations=iterations,
            seed=seed,
            dataset={"message_count": message_count, "message_size": message_size, "limit": 100},
            operation=operation,
            warmup=2,
        )
