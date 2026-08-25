"""Experiment 18: in-memory event stream can lose a notification.

``InMemoryEventStore.stream`` checks the event list, then waits on a condition
*outside* the lock. If a publisher holds the condition lock and notifies while
the stream is between the check and acquiring the lock, the notification has no
waiter yet — the stream then waits forever and never delivers the already
appended event (SSE stalls on a stale state until the next event, or forever).

This experiment reproduces the race deterministically: hold the condition lock,
notify, append the event, then release; an unfixed stream never delivers it.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from common import record_evidence, table

from agent_runner_contracts.events import EventEnvelope
from agentsupport.adapters.notification.event_store import InMemoryEventStore


async def run(phase: str) -> str:
    store = InMemoryEventStore()
    conversation_id = uuid4()
    condition = store._conditions[conversation_id]

    delivered: list[EventEnvelope] = []

    async def consume() -> None:
        async for event in store.stream(conversation_id, after_seq=0):
            delivered.append(event)

    # Holder simulates a publisher that already owns the condition lock.
    lock_held = asyncio.Event()

    async def holder() -> None:
        async with condition:
            lock_held.set()
            await asyncio.sleep(0.3)

    holder_task = asyncio.create_task(holder())
    await lock_held.wait()

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # consumer lists (empty) and blocks on the lock

    # Simulate the publisher's window: notify while holding the lock (no
    # waiter yet), then append the actual event, then release the lock.
    condition.notify_all()
    store.append(
        conversation_id,
        EventEnvelope(
            run_id=uuid4(),
            seq=1,
            type="message",
            payload={"lost": True},
        ),
    )
    holder_task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await holder_task

    await asyncio.sleep(0.3)  # give the stream a chance to deliver
    consumer.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await consumer

    rows = [
        ["events appended", 1],
        ["events delivered by stream", len(delivered)],
        ["delivered event types", [e.type for e in delivered]],
    ]
    markdown = "\n".join(
        [
            "## In-memory event stream lost-wakeup",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the stream misses the notification (it was not yet",
            "waiting) and never re-checks, so the appended event is not",
            "delivered. After the fix the stream re-checks under the lock and",
            "delivers the event.",
        ]
    )
    if phase == "after":
        assert len(delivered) == 1, f"delivered {len(delivered)} events"
    record_evidence(
        "exp18_event_store_lost_wakeup",
        phase,
        markdown,
        command=f"python devtools/experiments/exp18_event_store_lost_wakeup.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    asyncio.run(run(args.phase))
