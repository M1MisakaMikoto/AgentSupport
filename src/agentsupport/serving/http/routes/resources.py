from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from ....domain import PresetDefinition, ProjectConfig
from ..dependencies import agentsupport_service
from ..schemas import (
    ConversationCreate,
    OrganizationCreate,
    PresetCreate,
    PresetUpdate,
    ProjectCreate,
    ProjectImportPreset,
    ProjectUpdate,
    SessionCreate,
    UserCreate,
    WorkspaceCreate,
)

router = APIRouter()


@router.post("/organizations", status_code=201)
async def create_organization(
    request: Request,
    body: OrganizationCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_organization(
        body.name, idempotency_key
    )


@router.get("/organizations")
async def list_organizations(request: Request):
    return agentsupport_service(request).list_organizations()


@router.get("/organizations/{organization_id}")
async def get_organization(request: Request, organization_id: UUID):
    return agentsupport_service(request).get_organization(organization_id)


@router.get("/organizations/{organization_id}/users")
async def list_organization_users(request: Request, organization_id: UUID):
    return agentsupport_service(request).list_users(organization_id)


@router.post("/users", status_code=201)
async def create_user(
    request: Request,
    body: UserCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_user(
        body.username, body.organization_id, idempotency_key
    )


@router.get("/users/{user_id}")
async def get_user(request: Request, user_id: UUID):
    return agentsupport_service(request).get_user(user_id)


@router.get("/users")
async def list_users(request: Request):
    return agentsupport_service(request).list_users()


@router.get("/users/{user_id}/projects")
async def list_user_projects(request: Request, user_id: UUID):
    return agentsupport_service(request).list_projects(user_id)


@router.get("/users/{user_id}/presets")
async def list_user_presets(request: Request, user_id: UUID):
    return agentsupport_service(request).list_presets(user_id)


@router.get("/presets")
async def list_presets(request: Request, user_id: UUID | None = None):
    return agentsupport_service(request).list_presets(user_id)


@router.post("/presets", status_code=201)
async def create_preset(
    request: Request,
    body: PresetCreate,
    idempotency_key: str | None = Header(default=None),
):
    definition = (
        PresetDefinition(**body.definition.model_dump()) if body.definition else None
    )
    return agentsupport_service(request).create_preset(
        body.user_id, body.name, body.description, definition, idempotency_key
    )


@router.get("/presets/{preset_id}")
async def get_preset(request: Request, preset_id: UUID):
    return agentsupport_service(request).get_preset(preset_id)


@router.patch("/presets/{preset_id}")
async def update_preset(request: Request, preset_id: UUID, body: PresetUpdate):
    definition = (
        PresetDefinition(**body.definition.model_dump()) if body.definition else None
    )
    return agentsupport_service(request).update_preset(
        preset_id, name=body.name, description=body.description, definition=definition
    )


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(request: Request, preset_id: UUID):
    agentsupport_service(request).delete_preset(preset_id)
    return Response(status_code=204)


@router.post("/projects", status_code=201)
async def create_project(
    request: Request,
    body: ProjectCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_project(
        body.name, body.user_id, body.preset_id, idempotency_key
    )


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: UUID):
    return agentsupport_service(request).get_project(project_id)


@router.get("/projects")
async def list_projects(request: Request, user_id: UUID | None = None):
    return agentsupport_service(request).list_projects(user_id)


@router.patch("/projects/{project_id}")
async def update_project(
    request: Request, project_id: UUID, body: ProjectUpdate
):
    config = (
        ProjectConfig(**body.config.model_dump()) if body.config else None
    )
    return agentsupport_service(request).update_project(
        project_id, name=body.name, config=config
    )


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(request: Request, project_id: UUID):
    agentsupport_service(request).delete_project(project_id)
    return Response(status_code=204)


@router.get("/projects/{project_id}/sessions")
async def list_project_sessions(request: Request, project_id: UUID):
    agentsupport_service(request).get_project(project_id)
    return agentsupport_service(request).list_sessions(project_id)


@router.post("/projects/{project_id}/preset")
async def import_preset(
    request: Request, project_id: UUID, body: ProjectImportPreset
):
    service = agentsupport_service(request)
    project, previous_config = service.import_preset_to_project(
        project_id, body.preset_id
    )
    return {"project": project, "previous_config": previous_config}


@router.post("/projects/{project_id}/sessions", status_code=201)
async def create_project_session(
    request: Request,
    project_id: UUID,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_project_session(
        project_id, idempotency_key
    )


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: UUID):
    return agentsupport_service(request).get_session(session_id)


@router.get("/sessions/{session_id}/conversations")
async def list_session_conversations(request: Request, session_id: UUID):
    agentsupport_service(request).get_session(session_id)
    return agentsupport_service(request).list_conversations(session_id)


@router.get("/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: UUID):
    return agentsupport_service(request).get_conversation(conversation_id)


@router.post("/workspaces", status_code=201)
async def create_workspace(
    request: Request,
    body: WorkspaceCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_workspace(body.name, idempotency_key)


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreate,
    idempotency_key: str | None = Header(default=None),
):
    return agentsupport_service(request).create_session(body.workspace_id, idempotency_key)


@router.post("/sessions/{session_id}/conversations", status_code=201)
async def create_conversation(
    request: Request,
    session_id: UUID,
    body: ConversationCreate,
    idempotency_key: str | None = Header(default=None),
):
    return await agentsupport_service(request).create_conversation(
        session_id, body.task, body.parent_conversation_id, idempotency_key
    )
