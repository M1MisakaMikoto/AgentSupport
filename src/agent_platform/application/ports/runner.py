from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from agent_runner_contracts.checkpoint import Checkpoint
from agent_runner_contracts.events import EventEnvelope

EventSink = Callable[[EventEnvelope], Awaitable[None]]


class CoreRuntime(Protocol):
    async def health(self, run_id: UUID | None = None) -> dict[str, Any]: ...

    async def run(self, request: dict[str, Any], event_sink: EventSink) -> dict[str, Any]: ...

    async def accept_input(
        self,
        run_id: UUID,
        interaction_id: str,
        value: Any,
        *,
        command_id: UUID | None = None,
    ) -> dict[str, Any]: ...

    async def accept_approval(
        self,
        run_id: UUID,
        approval_id: str,
        decision: str,
        *,
        command_id: UUID | None = None,
    ) -> dict[str, Any]: ...

    async def checkpoint(self, run_id: UUID, reason: str) -> Checkpoint: ...

    async def resume(
        self,
        checkpoint: Checkpoint,
        value: Any,
        event_sink: EventSink,
        *,
        command_id: UUID | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel(self, run_id: UUID, *, command_id: UUID | None = None) -> dict[str, Any]: ...
