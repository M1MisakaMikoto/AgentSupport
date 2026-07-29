from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


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
