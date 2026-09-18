"""Tenant preset API: upload, lookup, build trigger and internal build queue.

Presets are stored globally and addressed by ``tenant_id`` (no authentication).
Uploading a preset enqueues an asynchronous runner image build; a host-side
runner manager claims PENDING builds through the ``/internal`` endpoints and
reports the outcome back.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile

from ....application.service import ServiceError
from ....domain import MAX_CLI_PACKAGE_BYTES, BuildStatus
from ..dependencies import agentsupport_service

router = APIRouter()

MAX_METADATA_BYTES = 64 * 1024


def _parse_metadata(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ServiceError(
            "TENANT_PRESET_INVALID", "preset metadata exceeds the size limit", 413
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ServiceError(
            "TENANT_PRESET_INVALID", f"preset metadata is not valid JSON: {exc}", 422
        ) from exc
    if not isinstance(payload, dict):
        raise ServiceError(
            "TENANT_PRESET_INVALID", "preset metadata must be a JSON object", 422
        )
    return payload


async def _read_uploaded(files: list[UploadFile] | None) -> dict[str, bytes]:
    uploaded: dict[str, bytes] = {}
    for item in files or []:
        name = Path(item.filename or "").name
        if not name:
            raise ServiceError(
                "TENANT_PRESET_INVALID", "uploaded package must have a file name", 422
            )
        payload = await item.read(MAX_CLI_PACKAGE_BYTES + 1)
        if len(payload) > MAX_CLI_PACKAGE_BYTES:
            raise ServiceError(
                "TENANT_PRESET_PACKAGE_TOO_LARGE",
                f"package exceeds the size limit: {name}",
                413,
            )
        uploaded[name] = payload
    return uploaded


def _internal_service(request: Request):
    service = agentsupport_service(request)
    expected = str(getattr(service.config, "runner_manager_token", "") or "")
    if not expected:
        raise ServiceError(
            "INTERNAL_API_DISABLED",
            "runner manager is not configured; set AGENTSUPPORT_RUNNER_MANAGER_TOKEN",
            503,
        )
    provided = request.headers.get("X-Runner-Manager-Token") or ""
    if not provided or not secrets.compare_digest(provided, expected):
        raise ServiceError("INTERNAL_TOKEN_INVALID", "invalid runner manager token", 401)
    return service


@router.get("/tenants")
def list_tenant_presets(request: Request, tenant_id: str | None = None):
    presets = agentsupport_service(request).list_tenant_presets()
    if tenant_id is not None:
        presets = [item for item in presets if item.tenant_id == tenant_id]
    return [item.model_dump(mode="json") for item in presets]


@router.get("/tenants/{tenant_id}/preset")
def get_tenant_preset(request: Request, tenant_id: str):
    return agentsupport_service(request).get_tenant_preset(tenant_id).model_dump(
        mode="json"
    )


@router.put("/tenants/{tenant_id}/preset")
async def put_tenant_preset(
    request: Request,
    tenant_id: str,
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    service = agentsupport_service(request)
    result = service.put_tenant_preset(
        tenant_id,
        metadata=_parse_metadata(metadata),
        files=await _read_uploaded(files),
    )
    return result


@router.delete("/tenants/{tenant_id}/preset", status_code=204)
def delete_tenant_preset(request: Request, tenant_id: str) -> None:
    agentsupport_service(request).delete_tenant_preset(tenant_id)


@router.post("/tenants/{tenant_id}/preset/builds", status_code=202)
def start_preset_build(request: Request, tenant_id: str):
    return agentsupport_service(request).start_preset_build(tenant_id).model_dump(
        mode="json"
    )


@router.get("/tenants/{tenant_id}/preset/builds")
def list_preset_builds(
    request: Request,
    tenant_id: str,
    status: BuildStatus | None = None,
    limit: int = 100,
):
    return [
        item.model_dump(mode="json")
        for item in agentsupport_service(request).list_preset_builds(
            tenant_id=tenant_id, status=status, limit=limit
        )
    ]


@router.get("/tenants/{tenant_id}/preset/builds/{build_id}")
def get_preset_build(request: Request, tenant_id: str, build_id: UUID):
    return agentsupport_service(request).get_preset_build(
        tenant_id, build_id
    ).model_dump(mode="json")


@router.post("/internal/preset-builds/claim")
def claim_preset_build(request: Request):
    """Internal: the runner manager claims one PENDING build plus its payload."""

    service = _internal_service(request)
    claimed = service.claim_preset_build()
    if claimed is None:
        return {"build": None}
    return claimed


@router.post("/internal/preset-builds/{build_id}/complete")
def complete_preset_build(
    request: Request,
    build_id: UUID,
    body: dict[str, Any],
):
    service = _internal_service(request)
    try:
        status = BuildStatus(str(body.get("status") or "").upper())
    except ValueError as exc:
        raise ServiceError(
            "PRESET_BUILD_INVALID", "status must be READY or FAILED", 422
        ) from exc
    if status is BuildStatus.PENDING or status is BuildStatus.RUNNING:
        raise ServiceError(
            "PRESET_BUILD_INVALID", "status must be READY or FAILED", 422
        )
    build = service.complete_preset_build(
        build_id,
        status=status,
        image_tag=body.get("image_tag") or None,
        log_tail=str(body.get("log_tail") or ""),
        error=body.get("error") or None,
    )
    return build.model_dump(mode="json")
