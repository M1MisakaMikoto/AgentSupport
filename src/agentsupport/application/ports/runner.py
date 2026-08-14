from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from agent_runner_contracts.checkpoint import Checkpoint
from agent_runner_contracts.events import EventEnvelope
from agent_runner_contracts.registration import RunnerRegistration, RunnerRegistrationRequest

EventSink = Callable[[EventEnvelope], Awaitable[None]]


class CoreRuntime(Protocol):
    async def health(self, run_id: UUID | None = None) -> dict[str, Any]: ...

    async def model_connectivity(self) -> dict[str, Any]: ...

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


class RunnerRegistry(Protocol):
    """Directory of self-registered Runner services.

    The registry is authoritative for provider/capability-based routing. In
    PostgreSQL mode it must be shared across stateless API replicas.
    """

    def register(self, registration: RunnerRegistrationRequest, token_hash: str) -> RunnerRegistration: ...

    def heartbeat(
        self,
        runner_id: UUID,
        status: str,
        load: int,
        *,
        capabilities: list[str] | None = None,
        at: datetime | None = None,
    ) -> RunnerRegistration | None: ...

    def deregister(self, runner_id: UUID) -> bool: ...

    def get(self, runner_id: UUID) -> RunnerRegistration | None: ...

    def token_hash(self, runner_id: UUID) -> str | None: ...

    def list_ready(self, capabilities: set[str] | None = None) -> list[RunnerRegistration]: ...

    def expire_stale(
        self, *, at: datetime | None = None, timeout_seconds: float
    ) -> list[UUID]: ...
