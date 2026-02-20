from __future__ import annotations

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_send_message(bench_factory):
    seed = DEFAULT_SEED
    iterations = 50
    message_size = 512

    async with bench_factory("send_message", seed) as harness:

        async def operation(i: int) -> None:
            await harness.call_tool(
                "send_message",
                {
                    "project_key": harness.project_key,
                    "sender_name": harness.agent_name,
                    "to": [harness.agent_name],
                    "subject": f"Benchmark message {i}",
                    "body_md": make_message_body(seed, i, message_size),
                },
            )

        await run_benchmark(
            name="send_message",
            tool="send_message",
            iterations=iterations,
            seed=seed,
            dataset={"message_size": message_size, "recipients": 1},
            operation=operation,
            warmup=3,
        )
