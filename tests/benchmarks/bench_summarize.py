from __future__ import annotations

import pytest

from .utils import DEFAULT_SEED, make_message_body, run_benchmark


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_bench_summarize_thread(bench_factory):
    seed = DEFAULT_SEED
    iterations = 10
    message_size = 256
    message_count = 40
    thread_id = "bench-thread-001"

    async with bench_factory("summarize_thread", seed) as harness:

        agent_result = await harness.call_tool(
            "create_agent_identity",
            {
                "project_key": harness.project_key,
                "program": "benchmark",
                "model": "test",
                "task_description": "Secondary participant",
            },
        )
        agent_b = agent_result["name"]

        for i in range(message_count):
            sender = harness.agent_name if i % 2 == 0 else agent_b
            await harness.call_tool(
                "send_message",
                {
                    "project_key": harness.project_key,
                    "sender_name": sender,
                    "to": [harness.agent_name, agent_b],
                    "subject": f"Thread update {i}",
                    "body_md": f"Action item {i}: {make_message_body(seed, i, message_size)}",
                    "thread_id": thread_id,
                },
            )

        async def operation(_i: int) -> None:
            await harness.call_tool(
                "summarize_thread",
                {
                    "project_key": harness.project_key,
                    "thread_id": thread_id,
                    "include_examples": True,
                    "llm_mode": False,
                    "per_thread_limit": 50,
                },
            )

        await run_benchmark(
            name="summarize_thread",
            tool="summarize_thread",
            iterations=iterations,
            seed=seed,
            dataset={"message_count": message_count, "message_size": message_size, "thread_id": thread_id},
            operation=operation,
            warmup=1,
        )
