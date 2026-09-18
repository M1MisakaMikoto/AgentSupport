"""Tenant preset operations: upload, asynchronous image build, runtime policy.

A tenant preset is stored globally and looked up by ``tenant_id``. Uploading a
preset persists the metadata and the uploaded CLI packages, then immediately
enqueues an image build. A host-side ``runner_manager`` claims PENDING builds,
builds the tenant image and reports the result back through these same APIs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from ..domain import (
    DEFAULT_TENANT_ID,
    MAX_CLI_PACKAGE_BYTES,
    BuildStatus,
    CliApp,
    PresetBuild,
    TenantPreset,
    cli_policy_from_fields,
    image_tag_for,
    preset_content_hash,
    resolve_preset_fields,
    valid_cli_id,
)
from .common import ServiceError

logger = logging.getLogger(__name__)

_TENANT_ID_MAX = 120
_ENV_NAME = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PresetOpsMixin:
    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_tenant_presets(self) -> list[TenantPreset]:
        if self.repository:
            return self.repository.list_tenant_presets()
        return [self.tenant_presets[key] for key in sorted(self.tenant_presets)]

    def get_tenant_preset(self, tenant_id: str) -> TenantPreset:
        preset = self._load_preset(self._validate_tenant_id(tenant_id))
        if preset is None:
            raise ServiceError(
                "TENANT_PRESET_NOT_FOUND",
                f"tenant preset does not exist: {tenant_id}",
                404,
            )
        return preset

    def list_preset_builds(
        self,
        *,
        tenant_id: str | None = None,
        status: BuildStatus | None = None,
        limit: int = 100,
    ) -> list[PresetBuild]:
        if tenant_id is not None:
            self._validate_tenant_id(tenant_id)
        if self.repository:
            return self.repository.list_preset_builds(
                tenant_id=tenant_id, status=status, limit=limit
            )
        builds = [
            build
            for build in self.preset_builds.values()
            if (tenant_id is None or build.tenant_id == tenant_id)
            and (status is None or build.status == status)
        ]
        builds.sort(key=lambda item: item.created_at, reverse=True)
        return builds[:limit]

    def get_preset_build(self, tenant_id: str, build_id: UUID) -> PresetBuild:
        build = self._load_build(build_id)
        if build is None or build.tenant_id != tenant_id:
            raise ServiceError(
                "PRESET_BUILD_NOT_FOUND",
                f"preset build does not exist: {build_id}",
                404,
            )
        return build

    def latest_ready_image(self, tenant_id: str | None) -> str:
        """The image a session of ``tenant_id`` must run on (default when unknown)."""

        if tenant_id and tenant_id != DEFAULT_TENANT_ID:
            builds = self.list_preset_builds(
                tenant_id=tenant_id, status=BuildStatus.READY, limit=1
            )
            if builds and builds[0].image_tag:
                return builds[0].image_tag
        return str(getattr(self.config, "default_runner_image", "agentsupport-api"))

    # ------------------------------------------------------------------
    # Upload (PUT /tenants/{tenant_id}/preset)
    # ------------------------------------------------------------------

    def put_tenant_preset(
        self,
        tenant_id: str,
        *,
        metadata: dict[str, Any],
        files: dict[str, bytes],
    ) -> dict[str, Any]:
        tenant = self._validate_tenant_id(tenant_id)
        preset, digests = self._validate_preset_payload(tenant, metadata, files)
        content_hash = preset_content_hash(preset, digests)
        now = datetime.now(UTC)
        existing = self._load_preset(tenant)
        preset = preset.model_copy(
            update={
                "created_at": existing.created_at if existing else now,
                "updated_at": now,
            }
        )
        self._write_packages(tenant, preset, files, digests)
        self._save_preset(preset, content_hash)
        build = self.start_preset_build(tenant)
        logger.info(
            "tenant preset stored: tenant=%s cli_apps=%d hash=%s build=%s",
            tenant,
            len(preset.cli_apps),
            content_hash[:12],
            build.build_id,
        )
        return {
            "preset": preset.model_dump(mode="json"),
            "content_hash": content_hash,
            "build": build.model_dump(mode="json"),
        }

    def delete_tenant_preset(self, tenant_id: str) -> None:
        tenant = self._validate_tenant_id(tenant_id)
        if self.repository:
            removed = self.repository.delete_tenant_preset(tenant)
        else:
            removed = self.tenant_presets.pop(tenant, None) is not None
        if not removed:
            raise ServiceError(
                "TENANT_PRESET_NOT_FOUND",
                f"tenant preset does not exist: {tenant_id}",
                404,
            )
        shutil.rmtree(self._preset_dir(tenant), ignore_errors=True)

    # ------------------------------------------------------------------
    # Builds
    # ------------------------------------------------------------------

    def start_preset_build(self, tenant_id: str) -> PresetBuild:
        tenant = self._validate_tenant_id(tenant_id)
        preset = self._load_preset(tenant)
        if preset is None:
            raise ServiceError(
                "TENANT_PRESET_NOT_FOUND",
                f"tenant preset does not exist: {tenant_id}",
                404,
            )
        content_hash = self._preset_content_hash(preset)
        build = PresetBuild(tenant_id=tenant, content_hash=content_hash)
        self._save_build(build)
        return build

    def claim_preset_build(self) -> dict[str, Any] | None:
        """Internal: claim one PENDING build together with its build payload."""

        if self.repository:
            claimed = self.repository.claim_preset_build()
        else:
            pending = sorted(
                (
                    item
                    for item in self.preset_builds.values()
                    if item.status == BuildStatus.PENDING
                ),
                key=lambda item: item.created_at,
            )
            claimed = pending[0] if pending else None
            if claimed is not None:
                claimed = claimed.model_copy(
                    update={"status": BuildStatus.RUNNING, "started_at": datetime.now(UTC)}
                )
                self._save_build(claimed)
        if claimed is None:
            return None
        preset = self._load_preset(claimed.tenant_id)
        if preset is None:
            self._finish_build(
                claimed,
                status=BuildStatus.FAILED,
                error="tenant preset was removed before the build started",
            )
            return None
        return {
            "build": claimed.model_dump(mode="json"),
            "image_tag": image_tag_for(claimed.tenant_id, claimed.content_hash),
            "package_dir": str(self._preset_dir(claimed.tenant_id)),
            "cli_apps": [
                {
                    **app.model_dump(mode="json"),
                    "package_b64": self._package_b64(claimed.tenant_id, app),
                }
                for app in preset.cli_apps
            ],
            "env": list(preset.env),
        }

    def complete_preset_build(
        self,
        build_id: UUID,
        *,
        status: BuildStatus,
        image_tag: str | None = None,
        log_tail: str = "",
        error: str | None = None,
    ) -> PresetBuild:
        build = self._load_build(build_id)
        if build is None:
            raise ServiceError(
                "PRESET_BUILD_NOT_FOUND",
                f"preset build does not exist: {build_id}",
                404,
            )
        if status is BuildStatus.READY and not image_tag:
            raise ServiceError(
                "PRESET_BUILD_INVALID",
                "a READY build must report its image tag",
                422,
            )
        if status is BuildStatus.FAILED and not error:
            error = "build failed without a reported reason"
        return self._finish_build(
            build, status=status, image_tag=image_tag, log_tail=log_tail, error=error
        )

    # ------------------------------------------------------------------
    # Runtime policy
    # ------------------------------------------------------------------

    def cli_policy_for_session(self, session) -> dict[str, Any]:
        fields = self._resolved_fields(session.tenant_id)
        policy = cli_policy_from_fields(fields)
        policy["tenant_id"] = session.tenant_id or DEFAULT_TENANT_ID
        policy["image"] = self.latest_ready_image(session.tenant_id)
        return policy

    def preset_skills_for_session(self, session) -> list[str]:
        return list(self._resolved_fields(session.tenant_id)["skills"])

    async def ensure_runner(self, tenant_id: str | None) -> str | None:
        """Ask the host-side runner manager for a ready runner of this tenant."""

        url = str(getattr(self.config, "runner_manager_url", "") or "").strip()
        if not url:
            return None
        payload = {
            "tenant_id": tenant_id or DEFAULT_TENANT_ID,
            "image_tag": self.latest_ready_image(tenant_id),
        }
        token = str(getattr(self.config, "runner_manager_token", "") or "")
        headers = {"X-Runner-Manager-Token": token} if token else {}
        try:
            async with httpx.AsyncClient(timeout=float(getattr(
                self.config, "runner_manager_timeout_seconds", 120.0
            ))) as client:
                response = await client.post(
                    f"{url.rstrip('/')}/ensure-runner", json=payload, headers=headers
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - runner manager outages must not crash a run
            logger.warning("runner manager ensure-runner failed: %s", exc)
            return None
        endpoint = body.get("endpoint")
        return str(endpoint) if endpoint else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolved_fields(self, tenant_id: str | None) -> dict[str, Any]:
        preset = self._load_preset(tenant_id) if tenant_id else None
        default = self._load_preset(DEFAULT_TENANT_ID)
        return resolve_preset_fields(preset, default)

    def _validate_tenant_id(self, tenant_id: str) -> str:
        value = (tenant_id or "").strip()
        if (
            not value
            or len(value) > _TENANT_ID_MAX
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
        ):
            raise ServiceError(
                "TENANT_PRESET_INVALID", f"invalid tenant id: {tenant_id!r}", 422
            )
        return value

    def _validate_preset_payload(
        self, tenant_id: str, metadata: dict[str, Any], files: dict[str, bytes]
    ) -> tuple[TenantPreset, dict[str, str]]:
        if not isinstance(metadata, dict):
            raise ServiceError(
                "TENANT_PRESET_INVALID", "preset metadata must be a JSON object", 422
            )
        raw_apps = metadata.get("cli_apps") or []
        if not isinstance(raw_apps, list):
            raise ServiceError(
                "TENANT_PRESET_INVALID", "cli_apps must be a list", 422
            )
        apps: list[CliApp] = []
        seen: set[str] = set()
        for raw in raw_apps:
            try:
                app = CliApp.model_validate(raw).normalized()
            except ValidationError as exc:
                raise ServiceError(
                    "TENANT_PRESET_INVALID", f"invalid cli app: {exc}", 422
                ) from exc
            if not valid_cli_id(app.cli_id):
                raise ServiceError(
                    "TENANT_PRESET_INVALID", f"invalid cli id: {app.cli_id!r}", 422
                )
            if app.cli_id in seen:
                raise ServiceError(
                    "TENANT_PRESET_INVALID", f"duplicate cli id: {app.cli_id}", 422
                )
            seen.add(app.cli_id)
            if not app.entry:
                raise ServiceError(
                    "TENANT_PRESET_INVALID",
                    f"cli app {app.cli_id} must declare its entry command",
                    422,
                )
            if not app.package:
                raise ServiceError(
                    "TENANT_PRESET_INVALID",
                    f"cli app {app.cli_id} must declare its package file",
                    422,
                )
            if Path(app.package).name != app.package:
                raise ServiceError(
                    "TENANT_PRESET_INVALID",
                    f"package must be a plain file name: {app.package}",
                    422,
                )
            for name in app.env:
                if not _ENV_NAME.fullmatch(name):
                    raise ServiceError(
                        "TENANT_PRESET_INVALID",
                        f"invalid environment variable name: {name}",
                        422,
                    )
            if app.daemon is not None:
                if not app.daemon.command:
                    raise ServiceError(
                        "TENANT_PRESET_INVALID",
                        f"cli app {app.cli_id} declares an empty daemon command",
                        422,
                    )
                if app.daemon.port is not None and not (
                    1 <= app.daemon.port <= 65535
                ):
                    raise ServiceError(
                        "TENANT_PRESET_INVALID",
                        f"cli app {app.cli_id} daemon port must be 1..65535",
                        422,
                    )
            apps.append(app)

        digests: dict[str, str] = {}
        for app in apps:
            payload = files.get(app.package or "")
            if payload is None:
                raise ServiceError(
                    "TENANT_PRESET_PACKAGE_MISSING",
                    f"uploaded package is missing for cli app: {app.cli_id}",
                    422,
                )
            if len(payload) > MAX_CLI_PACKAGE_BYTES:
                raise ServiceError(
                    "TENANT_PRESET_PACKAGE_TOO_LARGE",
                    f"package for {app.cli_id} exceeds the size limit",
                    413,
                )
            digests[app.cli_id] = hashlib.sha256(payload).hexdigest()

        skills = [str(item) for item in (metadata.get("skills") or []) if str(item).strip()]
        if skills:
            try:
                self.skill_provider.resolve(skills, tenant_id=tenant_id)
            except (FileNotFoundError, ValueError) as exc:
                raise ServiceError("SKILL_NOT_FOUND", str(exc), 404) from exc

        env = [str(item) for item in (metadata.get("env") or []) if str(item).strip()]
        for name in env:
            if not _ENV_NAME.fullmatch(name):
                raise ServiceError(
                    "TENANT_PRESET_INVALID",
                    f"invalid environment variable name: {name}",
                    422,
                )

        now = datetime.now(UTC)
        preset = TenantPreset(
            tenant_id=tenant_id,
            name=str(metadata.get("name") or ""),
            description=str(metadata.get("description") or ""),
            cli_apps=apps,
            skills=skills,
            env=env,
            created_at=now,
            updated_at=now,
        )
        return preset, digests

    def _preset_dir(self, tenant_id: str) -> Path:
        root = Path(getattr(self.config, "presets_root", Path("presets"))).resolve()
        return root / tenant_id

    def _write_packages(
        self,
        tenant_id: str,
        preset: TenantPreset,
        files: dict[str, bytes],
        digests: dict[str, str],
    ) -> None:
        target_root = self._preset_dir(tenant_id)
        staging = target_root / f".staging-{hashlib.sha256(str(datetime.now(UTC).timestamp()).encode()).hexdigest()[:8]}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for app in preset.cli_apps:
                payload = files.get(app.package or "")
                if payload is None:
                    raise ServiceError(
                        "TENANT_PRESET_PACKAGE_MISSING",
                        f"uploaded package is missing for cli app: {app.cli_id}",
                        422,
                    )
                app_dir = staging / app.cli_id
                app_dir.mkdir(parents=True, exist_ok=True)
                (app_dir / str(app.package)).write_bytes(payload)
                (app_dir / "package.sha256").write_text(
                    digests[app.cli_id], encoding="utf-8"
                )
            if target_root.exists():
                for child in target_root.iterdir():
                    if child.name == staging.name:
                        continue
                    shutil.rmtree(child, ignore_errors=True)
            else:
                target_root.mkdir(parents=True, exist_ok=True)
            for child in staging.iterdir():
                shutil.move(str(child), str(target_root / child.name))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _package_b64(self, tenant_id: str, app: CliApp) -> str:
        path = self._preset_dir(tenant_id) / app.cli_id / str(app.package)
        if not path.is_file():
            raise ServiceError(
                "TENANT_PRESET_PACKAGE_MISSING",
                f"stored package is missing for cli app: {app.cli_id}",
                409,
            )
        return base64.b64encode(path.read_bytes()).decode("ascii")

    def _preset_content_hash(self, preset: TenantPreset) -> str:
        digests: dict[str, str] = {}
        for app in preset.cli_apps:
            path = self._preset_dir(preset.tenant_id) / app.cli_id / "package.sha256"
            if path.is_file():
                digests[app.cli_id] = path.read_text(encoding="utf-8").strip()
        return preset_content_hash(preset, digests)

    def _load_preset(self, tenant_id: str) -> TenantPreset | None:
        if self.repository:
            preset = self.repository.get_tenant_preset(tenant_id)
            if preset:
                self.tenant_presets[tenant_id] = preset
            return preset
        return self.tenant_presets.get(tenant_id)

    def _save_preset(self, preset: TenantPreset, content_hash: str) -> None:
        if self.repository:
            self.repository.save_tenant_preset(preset, content_hash)
        self.tenant_presets[preset.tenant_id] = preset
        self.preset_hashes[preset.tenant_id] = content_hash

    def _load_build(self, build_id: UUID) -> PresetBuild | None:
        if self.repository:
            build = self.repository.get_preset_build(build_id)
            if build:
                self.preset_builds[build.build_id] = build
            return build
        return self.preset_builds.get(build_id)

    def _save_build(self, build: PresetBuild) -> None:
        if self.repository:
            self.repository.save_preset_build(build)
        self.preset_builds[build.build_id] = build

    def _finish_build(
        self,
        build: PresetBuild,
        *,
        status: BuildStatus,
        image_tag: str | None = None,
        log_tail: str = "",
        error: str | None = None,
    ) -> PresetBuild:
        finished = build.model_copy(
            update={
                "status": status,
                "image_tag": image_tag or build.image_tag,
                "log_tail": log_tail[-4000:],
                "error": error,
                "finished_at": datetime.now(UTC),
            }
        )
        self._save_build(finished)
        return finished


__all__ = ["PresetOpsMixin"]
