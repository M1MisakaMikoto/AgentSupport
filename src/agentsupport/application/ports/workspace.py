from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class WorkspaceProvider(Protocol):
    def create(self, name: str) -> tuple[UUID, str]: ...

    def path(self, workspace_id: UUID) -> str: ...


class WorkspaceStorageDriver(WorkspaceProvider, Protocol):
    def create_version(self, workspace_id: UUID, **options: Any) -> str: ...

    def list_versions(self, workspace_id: UUID) -> list[dict[str, Any]]: ...

    def restore_version(self, workspace_id: UUID, version_id: str) -> None: ...
