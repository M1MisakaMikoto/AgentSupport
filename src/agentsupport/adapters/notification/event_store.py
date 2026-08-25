from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope


class InMemoryEventStore:
    """Authoritative-in-process event store used by the first local adapter."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[EventEnvelope]] = defaultdict(list)
        self._conditions: dict[UUID, asyncio.Condition] = defaultdict(asyncio.Condition)

    def append(self, conversation_id: UUID, event: EventEnvelope) -> EventEnvelope:
        events = self._events[conversation_id]
        expected = len(events) + 1
        if event.seq != expected:
            raise ValueError(f"event sequence conflict: expected {expected}, got {event.seq}")
        events.append(event)
        return event

    def list(self, conversation_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        return [event for event in self._events.get(conversation_id, []) if event.seq > after_seq]

    async def publish(self, conversation_id: UUID, event: EventEnvelope) -> None:
        condition = self._conditions[conversation_id]
        async with condition:
            condition.notify_all()

    async def stream(
        self, conversation_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        cursor = after_seq
        while True:
            events = self.list(conversation_id, cursor)
            if events:
                for event in events:
                    cursor = event.seq
                    yield event
                continue
            condition = self._conditions[conversation_id]
            async with condition:
                # Re-check under the lock: a publisher that notified while we
                # were between the outer check and acquiring the lock would
                # otherwise be lost (no waiter yet), stalling the stream until
                # the next event or forever.
                events = self.list(conversation_id, cursor)
                if not events:
                    await condition.wait()
                    continue
            for event in events:
                cursor = event.seq
                yield event
