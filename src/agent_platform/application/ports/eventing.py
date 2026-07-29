from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope


class EventStore(Protocol):
    def append(self, conversation_id: UUID, event: EventEnvelope) -> EventEnvelope: ...

    def list(self, conversation_id: UUID, after_seq: int = 0) -> list[EventEnvelope]: ...

    async def publish(self, conversation_id: UUID, event: EventEnvelope) -> None: ...

    def stream(
        self, conversation_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]: ...


class EventNotifier(Protocol):
    async def publish(self, topic: str, aggregate_id: UUID, payload: dict[str, Any]) -> None: ...

    async def wait(self, topic: str, aggregate_id: UUID, timeout: float) -> bool: ...

    async def close(self) -> None: ...
