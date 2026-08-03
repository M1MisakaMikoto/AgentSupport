from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4


class LocalWorkspaceProvider:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._paths: dict[UUID, Path] = {}

    def create(self, name: str) -> tuple[UUID, str]:
        workspace_id = uuid4()
        path = self.root / str(workspace_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / ".workspace").write_text(name + "\n", encoding="utf-8")
        self._paths[workspace_id] = path
        return workspace_id, str(path)

    def path(self, workspace_id: UUID) -> str:
        try:
            return str(self._paths[workspace_id])
        except KeyError as exc:
            raise KeyError(f"unknown workspace: {workspace_id}") from exc


class LocalWorkspaceStorageDriver(LocalWorkspaceProvider):
    """Local workspace provider without snapshot support."""

    def create_version(self, workspace_id: UUID, **options: object) -> str:
        self.path(workspace_id)
        raise NotImplementedError("workspace version snapshots are not implemented")

    def restore_version(self, workspace_id: UUID, version_id: str) -> None:
        self.path(workspace_id)
        raise NotImplementedError("workspace version restore is not implemented")


class KubernetesWorkspaceProvider:
    """Creates stable PVC references without writing API-local filesystem state."""

    @staticmethod
    def _reference(workspace_id: UUID) -> str:
        return f"pvc://workspace-{workspace_id}"

    def create(self, name: str) -> tuple[UUID, str]:
        del name
        workspace_id = uuid4()
        return workspace_id, self._reference(workspace_id)

    def path(self, workspace_id: UUID) -> str:
        return self._reference(workspace_id)
