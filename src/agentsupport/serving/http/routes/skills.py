"""Skill self-service API: upload, discover, inspect and remove skills.

``tenant_id`` (form field on upload, query parameter elsewhere, or the
``X-Tenant-Id`` header — the convention the skill-generation endpoints already
use) selects the tenant namespace. Without it the shared namespace is used, so
existing callers keep their behaviour.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from ....application.service import ServiceError
from ....observability import metrics as obs_metrics
from ..dependencies import agentsupport_service

router = APIRouter()

MAX_UPLOAD_SIZE = 2 * 1024 * 1024


def _tenant_scope(request: Request, tenant_id: str | None) -> str | None:
    value = (tenant_id or request.headers.get("X-Tenant-Id") or "").strip()
    return value or None


@router.post("/skills", status_code=201)
async def create_skill(
    request: Request,
    skill_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    tenant_id: Annotated[str | None, Form()] = None,
):
    payload = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(payload) > MAX_UPLOAD_SIZE:
        raise ServiceError("SKILL_TOO_LARGE", "skill package exceeds upload size limit", 413)
    result = agentsupport_service(request).create_skill(
        skill_id,
        filename=file.filename or "SKILL.md",
        payload=payload,
        tenant_id=_tenant_scope(request, tenant_id),
    )
    obs_metrics.record_skill_upload()
    return result


@router.get("/skills")
def list_skills(request: Request, tenant_id: str | None = None):
    return agentsupport_service(request).list_skills(
        tenant_id=_tenant_scope(request, tenant_id)
    )


@router.get("/skills/{skill_id}")
def get_skill(request: Request, skill_id: str, tenant_id: str | None = None):
    return agentsupport_service(request).get_skill(
        skill_id, tenant_id=_tenant_scope(request, tenant_id)
    )


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(request: Request, skill_id: str, tenant_id: str | None = None) -> None:
    agentsupport_service(request).delete_skill(
        skill_id, tenant_id=_tenant_scope(request, tenant_id)
    )
