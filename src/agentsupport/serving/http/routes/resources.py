from uuid import UUID

from fastapi import APIRouter, Header, Request

from ..dependencies import agentsupport_service
from ..schemas import ConversationCreate, SessionCreate, WorkspaceCreate

router = APIRouter()


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
