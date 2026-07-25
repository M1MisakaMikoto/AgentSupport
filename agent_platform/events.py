from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    schema_version: str = "1"
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    seq: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "platform"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
                await condition.wait()
