"""v0.2 public resource API: Workspaces, Sessions, Conversations.

Business entities (organizations, users, presets, projects) are managed by the
upstream caller; this platform only stores their identifiers as optional labels.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from ....domain import PresetSkill, ProjectConfig
from ..dependencies import agentsupport_service
from ..schemas import (
    ConversationCreate,
    SessionCreate,
    WorkspaceCreate,
)

router = APIRouter()


def _created(entity: BaseModel, auto_created: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = entity.model_dump(mode="json")
    if auto_created:
        payload["auto_created"] = auto_created
    return payload


def _session_labels(
    body: SessionCreate,
    *,
    x_tenant: str | None,
    x_user: str | None,
    x_project: str | None,
) -> dict[str, str | None]:
    """Identity headers from the gateway win over request-body labels."""

    return {
        "tenant_id": x_tenant or body.tenant_id,
        "user_id": x_user or body.user_id,
        "project_id": x_project or body.project_id,
    }


@router.post("/workspaces", status_code=201)
async def create_workspace(
    request: Request,
    body: WorkspaceCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_workspace(
        body.name, idempotency_key
    ).model_dump(mode="json")


@router.get("/workspaces")
async def list_workspaces(request: Request):
    return [item.model_dump(mode="json") for item in agentsupport_service(request).list_workspaces()]


@router.get("/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: UUID):
    return agentsupport_service(request).get_workspace(workspace_id).model_dump(mode="json")


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreate,
    idempotency_key: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
):
    labels = _session_labels(
        body, x_tenant=x_tenant_id, x_user=x_user_id, x_project=x_project_id
    )
    auto_created: dict[str, Any] = {}
    session = agentsupport_service(request).create_session(
        body.workspace_id,
        idempotency_key,
        name=body.name,
        tenant_id=labels["tenant_id"],
        user_id=labels["user_id"],
        project_id=labels["project_id"],
        metadata=body.metadata,
        config=(
            ProjectConfig.model_validate(body.config.model_dump())
            if body.config is not None
            else None
        ),
        auto_created=auto_created,
    )
    return _created(session, auto_created)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    workspace_id: UUID | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
):
    return [
        item.model_dump(mode="json")
        for item in agentsupport_service(request).list_sessions(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
        )
    ]


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: UUID):
    return agentsupport_service(request).get_session(session_id).model_dump(mode="json")


@router.post("/sessions/{session_id}/conversations", status_code=201)
async def create_conversation(
    request: Request,
    session_id: UUID,
    body: ConversationCreate,
    idempotency_key: str | None = Header(default=None),
):
    auto_created: dict[str, Any] = {}
    conversation = await agentsupport_service(request).create_conversation(
        session_id,
        body.task,
        parent_conversation_id=body.parent_conversation_id,
        idempotency_key=idempotency_key,
        workspace_id=body.workspace_id,
        skills=(
            [PresetSkill(skill_id=item.skill_id, enabled=item.enabled) for item in body.skills]
            if body.skills is not None
            else None
        ),
        auto_created=auto_created,
    )
    return _created(conversation, auto_created)


@router.get("/sessions/{session_id}/conversations")
async def list_session_conversations(request: Request, session_id: UUID):
    service = agentsupport_service(request)
    service.get_session(session_id)
    return [
        item.model_dump(mode="json")
        for item in service.list_conversations(session_id=session_id)
    ]


@router.get("/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: UUID):
    return agentsupport_service(request).get_conversation(conversation_id).model_dump(mode="json")
