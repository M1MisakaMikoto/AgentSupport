from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from .domain import Checkpoint
from .events import EventEnvelope

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


class WorkspaceProvider(Protocol):
    def create(self, name: str) -> tuple[UUID, str]: ...

    def path(self, workspace_id: UUID) -> str: ...


class WorkspaceStorageDriver(WorkspaceProvider, Protocol):
    """Workspace files and deferred version-storage boundary."""

    def create_version(self, workspace_id: UUID, **options: Any) -> str: ...

    def restore_version(self, workspace_id: UUID, version_id: str) -> None: ...


class RuntimeDriver(Protocol):
    async def start(
        self,
        session_id: UUID,
        workspace_path: str,
        lease_epoch: int,
        workspace_id: UUID | None = None,
        read_only_mounts: list[tuple[str, str]] | None = None,
        runtime_operation_id: UUID | None = None,
    ) -> str: ...

    async def stop(self, container_id: str, *, force: bool = False) -> bool: ...

    async def inspect(self, container_id: str) -> dict[str, Any]: ...

    async def endpoint(self, container_id: str) -> str | None: ...
