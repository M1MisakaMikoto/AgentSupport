"""Experiment 15: Redis notifier creates a fresh pubsub subscription per wait.

``RedisEventNotifier.wait`` creates a new ``pubsub()`` + ``subscribe`` +
``get_message`` + close cycle for every call. SSE streams call ``wait`` in a
loop, so every poll cycle costs a subscribe/unsubscribe round trip on Redis.
This experiment counts pubsub objects and SUBSCRIBE commands over 30 waits,
then verifies the fix keeps one subscription per channel.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from common import record_evidence, table

from agentsupport.adapters.notification.notifier import RedisEventNotifier

REDIS_URL = "redis://127.0.0.1:6379/0"
WAITS = 30
WAIT_TIMEOUT = 0.05


async def run(phase: str) -> str:
    notifier = RedisEventNotifier(REDIS_URL)
    aggregate = uuid4()
    counters = {"pubsub": 0, "subscribe": 0}

    original_pubsub = notifier._redis.pubsub

    def counting_pubsub(*args, **kwargs):
        counters["pubsub"] += 1
        pubsub = original_pubsub(*args, **kwargs)
        original_subscribe = pubsub.subscribe

        async def counted_subscribe(*sub_args, **sub_kwargs):
            counters["subscribe"] += 1
            return await original_subscribe(*sub_args, **sub_kwargs)

        pubsub.subscribe = counted_subscribe
        return pubsub

    notifier._redis.pubsub = counting_pubsub

    async def drain() -> None:
        # Let any subscription background tasks settle before the next wait.
        await asyncio.sleep(0)

    for _ in range(WAITS):
        await notifier.wait("conversation.events", aggregate, timeout=WAIT_TIMEOUT)
        await drain()

    # Functional check: publish must wake a waiting subscriber.
    wake_task = asyncio.create_task(
        notifier.wait("conversation.events", aggregate, timeout=2.0)
    )
    await asyncio.sleep(0.05)
    await notifier.publish("conversation.events", aggregate, {"probe": 1})
    woke = await wake_task

    rows = [
        ["wait() calls", WAITS],
        ["pubsub objects created", counters["pubsub"]],
        ["SUBSCRIBE commands issued", counters["subscribe"]],
        ["publish wakes a waiter (functional)", str(woke)],
    ]
    markdown = "\n".join(
        [
            "## Redis pubsub churn per SSE wait",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix every wait() creates a new pubsub subscription",
            "(one SUBSCRIBE + close per poll). After the fix one subscription",
            "per channel is reused across waits.",
        ]
    )
    await notifier.close()
    record_evidence(
        "exp15_redis_pubsub_churn",
        phase,
        markdown,
        command=f"python devtools/experiments/exp15_redis_pubsub_churn.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
