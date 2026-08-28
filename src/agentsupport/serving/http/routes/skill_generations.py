"""Skill generation API: manual generation from session history and draft review."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from ....application.service import ServiceError
from ..dependencies import agentsupport_service
from ..schemas import SkillDraftReviewRequest

router = APIRouter()


@router.post("/sessions/{session_id}/skills/generate", status_code=201)
async def generate_skill(
    request: Request,
    session_id: UUID,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    generation = await agentsupport_service(request).generate_skill(
        session_id, tenant_id=x_tenant_id
    )
    return generation.model_dump(mode="json")


@router.get("/sessions/{session_id}/skills/generations/{generation_id}")
def get_generation(
    request: Request,
    session_id: UUID,
    generation_id: UUID,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    service = agentsupport_service(request)
    generation = service.get_skill_generation(generation_id, tenant_id=x_tenant_id)
    if generation.session_id != session_id:
        raise ServiceError(
            "GENERATION_NOT_FOUND", "skill generation does not exist", 404
        )
    return generation.model_dump(mode="json")


@router.get("/skill-drafts")
def list_drafts(
    request: Request,
    tenant_id: str | None = None,
    status: str | None = Query(
        default=None, pattern="^(draft|review|published|rejected)$"
    ),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    service = agentsupport_service(request)
    effective = (
        limit
        if limit is not None
        else (getattr(service.config, "list_default_limit", 100) or None)
    )
    return [
        draft.model_dump(mode="json")
        for draft in service.list_skill_drafts(
            tenant_id=tenant_id,
            status=status,
            limit=effective,
            offset=offset,
        )
    ]


@router.post("/skill-drafts/{draft_id}/review")
def review_draft(
    request: Request,
    draft_id: UUID,
    body: SkillDraftReviewRequest,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    return agentsupport_service(request).review_skill_draft(
        draft_id,
        tenant_id=x_tenant_id,
        decision=body.decision,
        note=body.note,
    ).model_dump(mode="json")
