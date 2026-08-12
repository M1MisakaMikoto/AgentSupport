"""Skill self-service API: upload, discover, inspect and remove skills."""

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from ....application.service import ServiceError
from ..dependencies import agentsupport_service

router = APIRouter()

MAX_UPLOAD_SIZE = 2 * 1024 * 1024


@router.post("/skills", status_code=201)
async def create_skill(
    request: Request,
    skill_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    payload = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(payload) > MAX_UPLOAD_SIZE:
        raise ServiceError("SKILL_TOO_LARGE", "skill package exceeds upload size limit", 413)
    return agentsupport_service(request).create_skill(
        skill_id,
        filename=file.filename or "SKILL.md",
        payload=payload,
    )


@router.get("/skills")
async def list_skills(request: Request):
    return agentsupport_service(request).list_skills()


@router.get("/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str):
    return agentsupport_service(request).get_skill(skill_id)


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(request: Request, skill_id: str) -> None:
    agentsupport_service(request).delete_skill(skill_id)
