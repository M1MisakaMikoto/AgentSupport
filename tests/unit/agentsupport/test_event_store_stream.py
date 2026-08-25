"""Regression: InMemoryEventStore.stream must not lose notifications."""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest

from agent_runner_contracts.events import EventEnvelope
from agentsupport.adapters.notification.event_store import InMemoryEventStore


def _event(seq: int) -> EventEnvelope:
    return EventEnvelope(run_id=uuid4(), seq=seq, type="message", payload={"i": seq})


@pytest.mark.asyncio
async def test_stream_delivers_event_notified_before_waiting():
    """A notification fired while the stream is between check and wait must
    not be lost: the stream re-checks under the lock."""

    store = InMemoryEventStore()
    conversation_id = uuid4()
    condition = store._conditions[conversation_id]
    delivered: list[EventEnvelope] = []

    async def consume() -> None:
        async for event in store.stream(conversation_id, after_seq=0):
            delivered.append(event)

    lock_held = asyncio.Event()

    async def holder() -> None:
        async with condition:
            lock_held.set()
            await asyncio.sleep(0.2)

    holder_task = asyncio.create_task(holder())
    await lock_held.wait()
    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.02)

    condition.notify_all()  # notification with no waiter yet
    store.append(conversation_id, _event(1))
    holder_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await holder_task

    await asyncio.sleep(0.1)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    assert [event.type for event in delivered] == ["message"]


@pytest.mark.asyncio
async def test_stream_delivers_events_published_normally():
    store = InMemoryEventStore()
    conversation_id = uuid4()
    delivered: list[EventEnvelope] = []

    async def consume() -> None:
        async for event in store.stream(conversation_id, after_seq=0):
            delivered.append(event)

    consumer = asyncio.create_task(consume())
    store.append(conversation_id, _event(1))
    await store.publish(conversation_id, _event(1))
    await asyncio.sleep(0.05)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    assert [event.seq for event in delivered] == [1]
