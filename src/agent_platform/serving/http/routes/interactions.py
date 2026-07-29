from uuid import UUID

from fastapi import APIRouter, Header, Request

from ..dependencies import platform_service
from ..schemas import ApprovalRequest, CancelRequest, InteractionRequest

router = APIRouter()


@router.post("/conversations/{conversation_id}/input")
async def submit_input(
    request: Request,
    conversation_id: UUID,
    body: InteractionRequest,
    idempotency_key: str | None = Header(default=None),
):
    return await platform_service(request).submit_input(
        conversation_id,
        body.interaction_id,
        body.value,
        body.expected_seq,
        idempotency_key,
    )


@router.post("/conversations/{conversation_id}/approval")
async def submit_approval(
    request: Request,
    conversation_id: UUID,
    body: ApprovalRequest,
    idempotency_key: str | None = Header(default=None),
):
    return await platform_service(request).submit_approval(
        conversation_id,
        body.approval_id,
        body.decision,
        body.expected_seq,
        idempotency_key,
    )


@router.post("/conversations/{conversation_id}/cancel")
async def cancel(
    request: Request,
    conversation_id: UUID,
    body: CancelRequest | None = None,
    idempotency_key: str | None = Header(default=None),
):
    return await platform_service(request).cancel(
        conversation_id,
        body.expected_seq if body else None,
        idempotency_key,
    )
