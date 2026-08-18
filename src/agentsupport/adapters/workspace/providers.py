from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ignore_internal(path: Path, names: list[str]) -> set[str]:
    """Exclude the platform's per-run bookkeeping from version snapshots."""

    return {".agentsupport"}


def _rmtree_force(path: Path) -> None:
    """Remove a directory tree, retrying read-only entries on Windows."""

    def _onerror(func, target, exc_info) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


class LocalWorkspaceProvider:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._paths: dict[UUID, Path] = {}

    def create(self, name: str, workspace_id: UUID | None = None) -> tuple[UUID, str]:
        workspace_id = workspace_id or uuid4()
        path = self.root / str(workspace_id)
        if path.exists():
            self._paths[workspace_id] = path
            return workspace_id, str(path)
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
    """Local workspace provider with directory-level version snapshots.

    Snapshots live outside the workspace itself (``root/.versions/<id>/``)
    so a restore replaces the full workspace contents with a clean baseline.
    Each snapshot is a plain directory copy plus a ``manifest.json`` carrying
    creation time and caller-supplied options (used by the eval layer to tag
    dataset versions).
    """

    VERSIONS_DIRNAME = ".versions"
    MANIFEST_NAME = "manifest.json"

    def _versions_dir(self, workspace_id: UUID) -> Path:
        return self.root / self.VERSIONS_DIRNAME / str(workspace_id)

    def _manifest(
        self,
        workspace_id: UUID,
        version_id: str,
        name: str | None,
        options: dict[str, object],
    ) -> dict[str, object]:
        return {
            "version_id": version_id,
            "workspace_id": str(workspace_id),
            "name": name,
            "created_at": _now_iso(),
            "options": options,
        }

    def create_version(self, workspace_id: UUID, **options: object) -> str:
        workspace = Path(self.path(workspace_id))
        version_id = uuid4().hex
        target = self._versions_dir(workspace_id) / version_id
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(
                workspace,
                target,
                ignore=_ignore_internal,
                symlinks=True,
            )
            manifest = self._manifest(
                workspace_id,
                version_id,
                options.pop("name", None),  # type: ignore[arg-type]
                dict(options),
            )
            (target / self.MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return version_id

    def list_versions(self, workspace_id: UUID) -> list[dict[str, object]]:
        base = self._versions_dir(workspace_id)
        if not base.is_dir():
            return []
        versions: list[dict[str, object]] = []
        for entry in base.iterdir():
            manifest_path = entry / self.MANIFEST_NAME
            if not entry.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {"version_id": entry.name}
            versions.append(manifest)
        versions.sort(key=lambda item: str(item.get("created_at", "")))
        return versions

    def restore_version(self, workspace_id: UUID, version_id: str) -> None:
        workspace = Path(self.path(workspace_id))
        source = self._versions_dir(workspace_id) / version_id
        if not source.is_dir() or not (source / self.MANIFEST_NAME).is_file():
            raise FileNotFoundError(f"workspace version does not exist: {version_id}")

        def _ignore_manifest(_path: Path, names: list[str]) -> set[str]:
            return {self.MANIFEST_NAME} if self.MANIFEST_NAME in names else set()

        _rmtree_force(workspace)
        shutil.copytree(source, workspace, ignore=_ignore_manifest, symlinks=True)


class KubernetesWorkspaceProvider:
    """Creates stable PVC references without writing API-local filesystem state."""

    @staticmethod
    def _reference(workspace_id: UUID) -> str:
        return f"pvc://workspace-{workspace_id}"

    def create(self, name: str, workspace_id: UUID | None = None) -> tuple[UUID, str]:
        del name
        workspace_id = workspace_id or uuid4()
        return workspace_id, self._reference(workspace_id)

    def path(self, workspace_id: UUID) -> str:
        return self._reference(workspace_id)
