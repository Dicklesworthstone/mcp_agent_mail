from __future__ import annotations

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_search_messages(bench_factory):
    seed = DEFAULT_SEED
    iterations = 15
    message_size = 256
    message_count = 300
    keywords = ["alpha", "beta", "gamma", "delta"]

    async with bench_factory("search_messages", seed) as harness:

        for i in range(message_count):
            keyword = keywords[i % len(keywords)]
            await harness.call_tool(
                "send_message",
                {
                    "project_key": harness.project_key,
                    "sender_name": harness.agent_name,
                    "to": [harness.agent_name],
                    "subject": f"{keyword} report {i}",
                    "body_md": f"{keyword} :: {make_message_body(seed, i, message_size)}",
                },
            )

        async def operation(_i: int) -> None:
            await harness.call_tool(
                "search_messages",
                {
                    "project_key": harness.project_key,
                    "query": "alpha OR beta",
                    "limit": 50,
                },
            )

        await run_benchmark(
            name="search_messages",
            tool="search_messages",
            iterations=iterations,
            seed=seed,
            dataset={"message_count": message_count, "message_size": message_size, "query": "alpha OR beta"},
            operation=operation,
            warmup=2,
        )
