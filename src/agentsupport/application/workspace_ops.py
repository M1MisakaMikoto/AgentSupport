"""Workspace operations: resolution, creation and version snapshots."""



from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ..domain import (
    Workspace,
)
from .common import (
    ServiceError,
    _hash_request,
)
from .ports import (
    RepositoryConflict,
)


class WorkspaceOpsMixin:

    def _resolve_workspace(
        self,
        workspace_id: UUID,
        *,
        name: str | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Workspace:
        workspace = None if self.temporal_mode else self.workspaces.get(workspace_id)
        if not workspace and self.repository:
            workspace = self.repository.get_workspace(workspace_id)
            if workspace:
                self.workspaces[workspace.id] = workspace
        if workspace:
            return workspace
        if not self._auto_create("workspace"):
            raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist", 404)
        return self.create_workspace(
            name or "auto",
            idempotency_key=None,
            workspace_id=workspace_id,
            auto_created=auto_created,
        )


    def get_workspace(self, workspace_id: UUID) -> Workspace:
        workspace = None if self.temporal_mode else self.workspaces.get(workspace_id)
        if not workspace and self.repository:
            workspace = self.repository.get_workspace(workspace_id)
            if workspace:
                self.workspaces[workspace.id] = workspace
        if not workspace:
            raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist", 404)
        return workspace


    def list_workspaces(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[Workspace]:
        if self.repository:
            return self.repository.list_workspaces(limit=limit, offset=offset)
        items = sorted(self.workspaces.values(), key=lambda item: item.created_at)
        if limit is not None:
            items = items[offset : offset + limit]
        return items


    # -- workspace version snapshots --------------------------------------
    def _workspace_storage_driver(self) -> Any:
        provider = self.workspace_provider
        if not all(
            hasattr(provider, name)
            for name in ("create_version", "list_versions", "restore_version")
        ):
            return None
        return provider


    def _require_workspace_storage_driver(self) -> Any:
        driver = self._workspace_storage_driver()
        if driver is None:
            raise ServiceError(
                "WORKSPACE_VERSIONING_UNSUPPORTED",
                "workspace storage does not support version snapshots",
                501,
            )
        return driver


    @staticmethod
    def _find_workspace_version(
        driver: Any, workspace_id: UUID, version_id: str
    ) -> dict[str, Any] | None:
        normalized = version_id.replace("-", "")
        for entry in driver.list_versions(workspace_id):
            if str(entry.get("version_id")).replace("-", "") == normalized:
                return entry
        return None


    def create_workspace_version(
        self,
        workspace_id: UUID,
        *,
        name: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(workspace_id)
        driver = self._require_workspace_storage_driver()
        payload = {"workspace_id": str(workspace_id), "name": name}
        existing = self._idempotent("workspace_version", idempotency_key, payload)
        if existing:
            version = self._find_workspace_version(driver, workspace_id, str(existing))
            if version is not None:
                return version
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "workspace_version", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                version = self._find_workspace_version(driver, workspace_id, str(existing))
                if version is not None:
                    self._remember("workspace_version", idempotency_key, payload, existing)
                    return version
        version_id = driver.create_version(workspace_id, name=name)
        response = {
            "workspace_id": str(workspace.id),
            "version_id": version_id,
            "name": name,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._remember_persisted(
            "workspace_version",
            idempotency_key,
            payload,
            UUID(version_id),
            response,
        )
        return response


    def list_workspace_versions(self, workspace_id: UUID) -> list[dict[str, Any]]:
        self.get_workspace(workspace_id)
        driver = self._require_workspace_storage_driver()
        return driver.list_versions(workspace_id)


    def _ensure_workspace_idle(self, workspace_id: UUID) -> None:
        lease_holder = self.workspace_leases.get(workspace_id)
        if lease_holder is not None:
            raise ServiceError(
                "WORKSPACE_BUSY",
                "workspace has an active session; restore is not allowed while a run is in flight",
                409,
            )
        if self.repository:
            sessions = self.repository.list_sessions()
            if any(
                session.workspace_id == workspace_id and session.active_container_id
                for session in sessions
            ):
                raise ServiceError(
                    "WORKSPACE_BUSY",
                    "workspace has an active session; restore is not allowed while a run is in flight",
                    409,
                )


    def restore_workspace_version(
        self,
        workspace_id: UUID,
        version_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(workspace_id)
        driver = self._require_workspace_storage_driver()
        payload = {"workspace_id": str(workspace_id), "version_id": version_id}
        existing = self._idempotent("workspace_version_restore", idempotency_key, payload)
        if existing:
            return {
                "workspace_id": str(workspace.id),
                "version_id": version_id,
                "restored_at": datetime.now(UTC).isoformat(),
                "idempotent_replay": True,
            }
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "workspace_version_restore", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember(
                    "workspace_version_restore", idempotency_key, payload, workspace.id
                )
                return {
                    "workspace_id": str(workspace.id),
                    "version_id": version_id,
                    "restored_at": datetime.now(UTC).isoformat(),
                    "idempotent_replay": True,
                }
        self._ensure_workspace_idle(workspace_id)
        try:
            driver.restore_version(workspace_id, version_id)
        except FileNotFoundError as exc:
            raise ServiceError(
                "WORKSPACE_VERSION_NOT_FOUND", "workspace version does not exist", 404
            ) from exc
        response = {
            "workspace_id": str(workspace.id),
            "version_id": version_id,
            "restored_at": datetime.now(UTC).isoformat(),
            "idempotent_replay": False,
        }
        self._remember_persisted(
            "workspace_version_restore",
            idempotency_key,
            payload,
            workspace.id,
            response,
        )
        return response


    def create_workspace(
        self,
        name: str,
        idempotency_key: str | None = None,
        *,
        workspace_id: UUID | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Workspace:
        if workspace_id is not None:
            existing = None if self.temporal_mode else self.workspaces.get(workspace_id)
            if not existing and self.repository:
                existing = self.repository.get_workspace(workspace_id)
                if existing:
                    self.workspaces[existing.id] = existing
            if existing:
                return existing
        payload = {"name": name}
        existing = self._idempotent("workspace", idempotency_key, payload)
        if existing:
            return self.workspaces[existing]
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "workspace", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                workspace = self.repository.get_workspace(existing)
                if workspace:
                    self.workspaces[workspace.id] = workspace
                    self._remember("workspace", idempotency_key, payload, workspace.id)
                    return workspace
        workspace_id, path = self.workspace_provider.create(name, workspace_id)
        workspace = Workspace(id=workspace_id, name=name, root_path=path)
        if self.repository:
            try:
                workspace = self.repository.create_workspace(
                    name,
                    path,
                    _hash_request(payload),
                    idempotency_key,
                    workspace_id=workspace_id,
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        self.workspaces[workspace.id] = workspace
        if auto_created is not None:
            auto_created["workspace"] = workspace.model_dump(mode="json")
        self._remember("workspace", idempotency_key, payload, workspace.id)
        return workspace
